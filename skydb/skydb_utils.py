from .crypto import hash_data_key, hash_all
from .crypto import encode_string, encode_num
from .crypto import genKeyPairFromSeed

import requests
from requests.exceptions import Timeout
import json
import nacl.bindings
import aiohttp

import os
import threading
from tenacity import retry, wait_fixed, retry_if_exception_type
from requests.exceptions import ReadTimeout as ReadTimeoutError
from urllib.parse import urljoin
import logging
import sys
import asyncio


def _equal(condition: dict, key: str, value: str, column_split: list = None) -> bool:
    return condition[key] == value


def _value_in(condition: dict, key: str, value: str, column_split: list) -> bool:
    idx = column_split.index(condition[key][0])
    value_list = value.split(';')
    return condition[key][1] == value_list[idx]


class SkydbTable(object):
    """
    - The main goals with this class will be to implement basic database functions such as add_rows,
    edit_rows, fetchone, fetchall
    """

    def __init__(self, table_name: str, columns: list, seed: str, column_split: list = [], verbose=0):
        """
        Args:
            table_name(str): This is the name of the table and will also act as key in the
            skydb registry.

            columns(list): This parameter will name all the columns of the table. In general I
            plan of setting each of the row as multiple key -> value pairs with key being the
            table_name:column_name:row_index and the value will be data stored at that (row i.e. index, column)
            place.

            seed(str): This is an important parameter. The seed will be used to generate the same
            public and private key pairs. If the seed is lost then access to the data entrys in the
            registry will also be lost.

            column_split(list): If you are making a single column hold all the values in the row seperated by
            ';', column_split will hold the column names for each of the single values
        """
        self.table_name = table_name
        self.seed = seed
        self.columns = columns
        self.column_split = column_split

        self.logger = logging.getLogger(__name__)
        self.logger.addHandler(logging.NullHandler())
        self.logger.setLevel(logging.DEBUG)

        if verbose:
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            ch = logging.StreamHandler(sys.stdout)
            ch.setFormatter(formatter)
            self.logger.addHandler(ch)

        # Initialize the Registry
        self._pk, self._sk = genKeyPairFromSeed(self.seed)
        self.registry = RegistryEntry(self._pk, self._sk, verbose=verbose)
        self.logger.debug("Initialized Table")

        # The index will be checked for and if there was no such table before then the index will be zero
        self.index, self._index_revision = self.get_index()

    @staticmethod
    def check_table(table_name: str, seed: str):
        """
            Given a table_name and seed, check if that table already exists in the Skydb.
        """
        pk, sk = genKeyPairFromSeed(seed)
        registry = RegistryEntry(pk, sk)
        try:
            index, revision = registry.get_entry(f"INDEX:{table_name}", timeout=5)
            return int(index), revision
        except Timeout as T:
            return None

    def calibrate_index(self):
        self.logger.debug("Inside calibrate_index function")
        index, revision = self.registry.get_entry(f"INDEX:{self.table_name}", timeout=5)
        self._index_revision = revision
        self.index = int(index)
        self.logger.debug("Calibrated Index to: " + str(index))

    def get_index(self) -> int:
        """
        - Check if the table existed before, if so then retrieve its index and return it else
        return 0. If a Timeout exception is raised then that means that the required data is not available at
        the moment.
        """
        self.logger.debug("Inside get index function")
        try:
            index, revision = self.registry.get_entry(f"INDEX:{self.table_name}", timeout=5)
            return int(index), revision
        except Timeout as T:
            self.logger.debug("Initializing the index...")
            self.registry.set_entry(data_key=f"INDEX:{self.table_name}", data=f"{0}", revision=1)
            return (0, 1)

    @retry(wait=wait_fixed(3), retry=retry_if_exception_type(ReadTimeoutError))
    def deprecated_add_row(self, row: dict) -> int:
        """
        Args:
            row(dict): this dictionary must have all the keys that have been passed as columns
            while initializing this object.
        Returns:
            latest_index(int): This value represents the index of the added row

        """
        # Check for invalid column names
        for k in row.keys():
            if k not in self.columns:
                raise ValueError("An invalid column has been passed.")

        # Check if all the columns are filled or not
        for k in self.columns:
            if k not in list(row.keys()):
                raise ValueError(f"Column {k} is empty")

        self.logger.debug("Adding row: ")
        self.logger.debug(row)
        # Add data to the registry one by one
        for key in row.keys():
            self.registry.set_entry(
                data_key=f"{self.table_name}:{key}:{self.index}",
                data=f"{row[key]}",
                revision=1
            )

        self.index += 1
        self.registry.set_entry(f"INDEX:{self.table_name}", f"{self.index}", self._index_revision + 1)
        self._index_revision += 1

        return self.index - 1


    @retry(wait=wait_fixed(3), retry=retry_if_exception_type(ReadTimeoutError))
    def add_row(self, row: dict) -> int:
        for k in row.keys():
            if k not in self.columns:
                raise ValueError("An invalid column has been passed.")

        # Check if all the columns are filled or not
        for k in self.columns:
            if k not in list(row.keys()):
                raise ValueError(f"Column {k} is empty")

        self.logger.debug("Adding row: ")
        self.logger.debug(row)
        tasks = [
            self.registry.aio_set_entry(
                data_key=f"{self.table_name}:{c}:{self.index}",
                data=f"{row[c]}",
                revision=1
            )
            for c in self.columns
        ]
        loop = asyncio.get_event_loop()
        task_list = asyncio.gather(*tasks)
        loop.run_until_complete(task_list)

        self.index += 1
        self.registry.set_entry(f"INDEX:{self.table_name}", f"{self.index}", self._index_revision + 1)
        self._index_revision += 1

        return self.index - 1


    @retry(wait=wait_fixed(3), retry=retry_if_exception_type(ReadTimeoutError))
    def add_rows(self, rows:list) -> list:
        for row in rows:
            for k in row.keys():
                if k not in self.columns:
                    raise ValueError("An invalid column has been passed.")

            # Check if all the columns are filled or not
            for k in self.columns:
                if k not in list(row.keys()):
                    raise ValueError(f"Column {k} is empty")

        tasks = []
        row_indices = []
        idx = self.index
        for row in rows:
            for c in row:
                tasks.append(
                    self.registry.aio_set_entry(
                        data_key=f"{self.table_name}:{c}:{idx}",
                        data=row[c],
                        revision=1
                    )
                )
            idx += 1
        self.index = idx
        tasks.append(
            self.registry.aio_set_entry(
                data_key=f"INDEX:{self.table_name}",
                data=f"{self.index}",
                revision=self._index_revision+1
            )
        )
        loop = asyncio.get_event_loop()
        tasks_list = asyncio.gather(*tasks)
        loop.run_until_complete(tasks_list)
        self._index_revision += 1
        return self.index - 1

    @retry(wait=wait_fixed(3), retry=retry_if_exception_type(ReadTimeoutError))
    def update_row(self, row_index: int, data: dict):
        """
        Args:
            row_index(int): The index of the row that you want to update.
            data(dict): The data that you want to update with.
        """

        self.calibrate_index()
        if row_index >= self.index or row_index < 0:
            raise ValueError(f"row_index={row_index} is invalid. It should in the range of 0-{self.index}")

        # Check for invalid column names
        for k in data.keys():
            if k not in self.columns:
                raise ValueError("An invalid column has been passed.")

        self.logger.debug("Updating row at index: " + str(row_index))
        self.logger.debug(data)
        for k in data.keys():
            old_data, revision = self.registry.get_entry(
                data_key=f"{self.table_name}:{k}:{row_index}",
            )
            self.registry.set_entry(
                data_key=f"{self.table_name}:{k}:{row_index}",
                data=f"{data[k]}",
                revision=revision + 1,
            )

    @retry(wait=wait_fixed(3), retry=retry_if_exception_type(ReadTimeoutError))
    def deprecated_fetch_row(self, row_index: int) -> dict:
        """
        Args:
            row_index(int): The index of the row that you want to fetch
        """
        self.calibrate_index()
        if row_index >= self.index or row_index < 0:
            raise ValueError(f"row_index={row_index} is invalid. It should in the range of 0-{self.index}")

        self.logger.debug("Fetching row at index: " + str(row_index))
        row = {}
        for c in self.columns:
            data, revision = self.registry.get_entry(data_key=f"{self.table_name}:{c}:{row_index}")
            row[c] = data
        return row

    @retry(wait=wait_fixed(3), retry=retry_if_exception_type(ReadTimeoutError))
    def fetch_row(self, row_index: int):
        self.calibrate_index()
        if row_index >= self.index or row_index < 0:
            raise ValueError(f"row_index={row_index} is invalid. It should in the range of 0-{self.index}")

        self.logger.debug("Fetching row at index: " + str(row_index))
        loop = asyncio.get_event_loop()
        tasks = [self.registry.aio_get_entry(f"{self.table_name}:{c}:{row_index}") \
                 for c in self.columns]
        tasks = asyncio.gather(*tasks)
        out = loop.run_until_complete(tasks)
        data = {}
        for item in out:
            data.update(item)
        final_data = {c.split(':')[1]: data[c][0] for c in data}
        return final_data

    @retry(wait=wait_fixed(3), retry=retry_if_exception_type(ReadTimeoutError))
    def fetch_rows(self, row_index_list: list):
        self.calibrate_index()
        for row_index in row_index_list:
            if row_index >= self.index or row_index < 0:
                raise ValueError("Invalid row_index passed")

        tasks = []
        for row_index in row_index_list:
            for c in self.columns:
                tasks.append(self.registry.aio_get_entry(f"{self.table_name}:{c}:{row_index}"))
        loop = asyncio.get_event_loop()
        tasks_list = asyncio.gather(*tasks)
        out = loop.run_until_complete(tasks_list)
        data = {}
        for item in out:
            key = next(iter(item))
            row_index = int(key.split(':')[-1])
            col = key.split(':')[1]
            if row_index not in data.keys():
                data[row_index] = {}
            data[row_index][col] = item[key][0]
        return data


    @retry(wait=wait_fixed(3), retry=retry_if_exception_type(ReadTimeoutError))
    async def _fetch(self, condition: dict, n_rows: int, work_index: int, condition_func):
        """
        This function will be run asynchronously. Will check through each column of the row and see if it
        matches the condition
        Args:
            condition(dict): The column values that we need to match
            n_rows(int): The max rows that we need to fetch
            work_index(int): The current working index of the function

        """

        keys_satisfy = False
        if work_index < 0 or work_index >= self.index or len(self.rows_to_fetch) >= n_rows:
            """
                - If the thread is on an index which is more that the no.of rows in the table or an 
                index which is less than zero.
                - If we have reached the max no.of rows that we needed to fetch
            """
            return

        """ For each of the given condition, check if the row at work_index matches the condition """
        for k in condition.keys():
            resp_data = await self.registry.aio_get_entry(
                data_key=f"{self.table_name}:{k}:{work_index}"
            )
            data, revision = resp_data[f"{self.table_name}:{k}:{work_index}"]
            if condition_func(condition, k, data,
                              self.column_split):  # The value at the column matches the condition
                keys_satisfy = True
            else:
                keys_satisfy = False
                break

        if keys_satisfy:
            """ The condition match """
            self.fetch_lock.acquire()
            if len(self.rows_to_fetch) < n_rows:
                self.rows_to_fetch.append(work_index)
            self.fetch_lock.release()

    @retry(wait=wait_fixed(3), retry=retry_if_exception_type(ReadTimeoutError))
    def fetch(self, condition: dict, start_index: int, n_rows: int = 2, condition_func=None) -> dict:
        """
        - This function will fetch a row or bunch of rows, which satifies the condition. The condition can be something like
        {'c1':'data 1', 'c2':'JeJa'}. The rows with value 'data 1' at column c1 and value 'JeJa' at column c2
        will be matched and returned.

        - This function searches the rows in descending order, for example if the start_index=28, the function
        will search for rows that match the condition from row 28 all the way to row 0, until the no.of rows
        matched are equal to n_rows.

        Args:
            condition(dict): This variable is basically the values that will be in the row
            that you want to fetch

            start_index(int): The index from where the searching should start.

            n_rows(int): This variable specifies the no.of rows that I need to fetch at max in this fetch_operation.

            condition_func: A function which takes condition, k, target_value and columns as arguments. You can use this
            function along with the conditions so that a row matches that condition.

        """
        self.calibrate_index()
        # Make sure the condition is not empty
        assert len(condition) > 0, "The condition should not be empty"

        # Make sure that the start_index is not greater latest record and not less than zero
        assert start_index in range(0, self.index), \
            f"The start_index:{start_index} is invalid. It should in the range [0,{self.index})."

        # Check if the keys are valid column names
        for k in condition.keys():
            assert (k in self.columns or k in self.column_split), f"Invalid column name: {k}"

        self.fetch_lock = threading.Lock()
        self.rows_to_fetch = []
        self.logger.debug("In function fetch. Recieved arguments: ")
        self.logger.debug(condition)
        self.logger.debug(
            "Start Index: " + str(start_index) + " n_rows: " + str(n_rows))
        if condition_func is None:
            condition_func = _equal

        while True:
            if start_index < 0:
                break
            tasks = [
                self._fetch(condition, n_rows, start_index-i, condition_func)
                for i in range(5)
            ]
            loop = asyncio.get_event_loop()
            tasks_list = asyncio.gather(*tasks)
            loop.run_until_complete(tasks_list)
            if len(self.rows_to_fetch) >= n_rows:
                break
            start_index = start_index - 5

        out = self.fetch_rows(self.rows_to_fetch[:n_rows])
        return out


class RegistryEntry(object):

    def __init__(self, public_key: bytes, private_key: bytes,
                 prefix_endpoint_url: str = os.getenv('REGISTRY_URL', "https://siasky.net/"),
                 verbose=0,
                 ):
        """
        Args:
            private_key(bytes), public_key(bytes): These two keys are responsible to sign and verify the
            messages that will be sent and retreived from the skynet

        """

        self._pk = public_key
        self._sk = private_key
        if prefix_endpoint_url != "":
            self._endpoint_url = urljoin(prefix_endpoint_url, "skynet/registry")
        else:
            self._endpoint_url = urljoin("http://siasky.net/", "skynet/registry")

        # This below variable refers to max size of the signed message
        self._max_len = 64
        self._max_data_size = 113

        # Logger
        self.logger = logging.getLogger(__name__)
        self.logger.addHandler(logging.NullHandler())
        self.logger.setLevel(logging.DEBUG)

        if verbose:
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            ch = logging.StreamHandler(sys.stdout)
            ch.setFormatter(formatter)
            self.logger.addHandler(ch)
        self.logger.debug("Using endpoint url: " + self._endpoint_url)

    def set_entry(self, data_key: str, data: str, revision: int) -> bool:
        """
            - This function is based on the setEntry function of registry.ts.
            - Basically add an entry into the skynet with data_key as the key

        """
        # Make sure that the data size does not exceed the max bytes
        assert len(
            data) <= self._max_data_size, f"The data size({len(data)}) exceeded the limit of {self._max_data_size}."

        self.logger.debug("Inside set Entry function")

        # First sign the data
        hash_entry = hash_all((
            list(bytearray.fromhex(hash_data_key(data_key))),
            encode_string(data),
            encode_num(revision),
        ))
        raw_signed = nacl.bindings.crypto_sign(hash_entry, self._sk)

        # The public key needs to be encoded into a list of integers. Basically convert hex -> bytes
        public_key = {'algorithm': "ed25519", 'key': list(self._pk)}

        _data_key = hash_data_key(data_key)
        _data = list(data.encode())
        _signature = list(raw_signed)[:self._max_len]

        post_data = {
            'publickey': public_key,
            'datakey': _data_key,
            'revision': revision,
            'data': _data,
            'signature': _signature,
        }

        response = requests.post(self._endpoint_url, data=json.dumps(post_data))
        if response.status_code == 204:
            self.logger.debug("Data Successfully stored in the Registry")
        else:
            self.logger.debug(response.text)
            raise Exception("""
            The Registry Data was Invalid. Please do recheck that 
            - you are not using the same revision number to update the data. 
            - make sure that the keys used to sign the message come from the same seed value.
            """)

    def get_entry(self, data_key: str, timeout: int = 30) -> str:
        """
            - Get the entry given the dataKey
        """
        self.logger.debug("Inside get Entry function")
        self.logger.debug("Data Key:")
        self.logger.debug(data_key)
        self.logger.debug("Timeout:")
        self.logger.debug(data_key)
        self.logger.debug(timeout)
        publickey = f"ed25519:{self._pk.hex()}"
        datakey = hash_data_key(data_key)
        querry = {
            'publickey': publickey,
            'datakey': datakey,
        }
        # The below line will raise requests.exceptions.Timeout exception if it was unable to fetch the data
        # in two seconds.
        response = requests.get(self._endpoint_url, params=querry, timeout=timeout)
        self.logger.debug("Status Code: ")
        self.logger.debug(response.status_code)
        self.logger.debug("Status Text: ")
        self.logger.debug(response.text)
        response_data = json.loads(response.text)
        if 'revision' not in response_data.keys():
            return ("0", 0)
        revision = response_data['revision']
        data = bytearray.fromhex(response_data['data']).decode()
        return (data, revision)

    async def aio_set_entry(self, data_key: str, data: str, revision: int):
        # Make sure that the data size does not exceed the max bytes
        assert len(
            data) <= self._max_data_size, f"The data size({len(data)}) exceeded the limit of {self._max_data_size}."

        self.logger.debug("Inside set Entry function")

        # First sign the data
        hash_entry = hash_all((
            list(bytearray.fromhex(hash_data_key(data_key))),
            encode_string(data),
            encode_num(revision),
        ))
        raw_signed = nacl.bindings.crypto_sign(hash_entry, self._sk)

        # The public key needs to be encoded into a list of integers. Basically convert hex -> bytes
        public_key = {'algorithm': "ed25519", 'key': list(self._pk)}

        _data_key = hash_data_key(data_key)
        _data = list(data.encode())
        _signature = list(raw_signed)[:self._max_len]

        post_data = {
            'publickey': public_key,
            'datakey': _data_key,
            'revision': revision,
            'data': _data,
            'signature': _signature,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(self._endpoint_url, data=json.dumps(post_data)) as response:
                if response.status == 204:
                    self.logger.debug("Data Successfully stored")
                else:
                    self.logger.debug(response.text())
                    raise Exception("""
                    The Registry Data was Invalid. Please do recheck that 
                    - you are not using the same revision number to update the data. 
                    - make sure that the keys used to sign the message come from the same seed value.
                    """)


    async def aio_get_entry(self, data_key: str, timeout: int = 30,) -> str:
        """
            - Used aio requests to get data from skydb
        """
        self.logger.debug("Inside async get Entry function")
        publickey = f"ed25519:{self._pk.hex()}"
        datakey = hash_data_key(data_key)
        querry = {
            'publickey': publickey,
            'datakey': datakey,
        }
        # The below line will raise requests.exceptions.Timeout exception if it was unable to fetch the data
        # in two seconds.

        async with aiohttp.ClientSession() as session:
            async with session.get(self._endpoint_url, params=querry) as response:
                json_text = await response.text()
                response_data = json.loads(json_text)
                revision = response_data['revision']
                data = bytearray.fromhex(response_data['data']).decode()
                return {data_key: [data, revision]}
