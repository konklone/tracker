import itertools
from http import HTTPStatus
from time import sleep
import typing
import pymongo
from pymongo import UpdateOne


# Data loads should clear the entire database first.
def _clear_collection(client: pymongo.MongoClient, name: str, database: typing.Optional[str] = None):
    client.get_database(database).get_collection('meta').delete_many({'_collection': name})


class InsertionError(Exception):
    def __init__(self, *args, errors, **kwargs):
        super().__init__(*args, **kwargs)
        self.errors = errors


def grouper(group_size, iterable):
    iterator = iter(iterable)
    while True:
        chunk = tuple(itertools.islice(iterator, group_size))
        if not chunk:
            return
        yield chunk

T = typing.TypeVar('T')
def _retry_write(
        data: T,
        write_method: typing.Callable[[T], None],
        times: int
    ) -> None:
    '''Attempt `collection`.insert_many(`documents`) `times` times'''

    errors = []
    for count in range(1, times+1): # Only do {times} attempts to insert
        try:
            write_method(data)
            break
        except pymongo.errors.DuplicateKeyError as exc:
            # After retrying the insertion, some of the documents were duplicates, this is OK
            break
        except pymongo.errors.BulkWriteError as exc:
            details = exc.details.get('writeErrors', [])
            # Check if all errors were duplicate key errors, if so this is OK
            if not all(error['code'] == 11000 for error in details):
                raise exc
            break
        except pymongo.errors.OperationFailure as exc:
            # Check if we blew the request rate, if so take a break and try again
            errors.append(exc)
            if exc.code == HTTPStatus.TOO_MANY_REQUESTS:
                sleep(count)
            else:
                raise
    else:
        # Loop exited normally, not via a break. This means that it failed each time
        raise InsertionError("Unable to insert document, failed %d times" % count, errors=errors)
    return



def _insert_all(
        client: pymongo.MongoClient,
        collection: str,
        documents: typing.Iterable[typing.Dict],
        database: typing.Optional[str] = None,
        batch_size: typing.Optional[int] = None) -> None:
    if not batch_size:
        client.get_database(database)\
              .get_collection('meta')\
              .insert_many({'_collection': collection, **document} for document in documents)
    else:
        document_stream = grouper(batch_size, documents)
        collect = client.get_database(database).get_collection('meta')
        for chunk in document_stream:
            documents = [{'_collection': collection, **document} for document in chunk]
            _retry_write(documents, collect.insert_many, 5)


def _insert(
        client: pymongo.MongoClient,
        collection: str,
        document: typing.Dict,
        database: typing.Optional[str] = None) -> None:
    client.get_database(database).get_collection('meta').insert_one({'_collection': collection, **document})


def _upsert_all(
        client: pymongo.MongoClient,
        collection: str,
        documents: typing.Iterable[typing.Dict],
        key_col: str = '_id',
        database: typing.Optional[str] = None,
        batch_size: typing.Optional[int] = None) -> None:

    writes = [
        UpdateOne(
            {'_collection': collection, key_col: document.get(key_col)},
            {'$set': {'_collection': collection, **document}},
            upsert=True,
        ) for document in documents
    ]

    if batch_size:
        client.get_database(database)\
              .get_collection('meta')\
              .bulk_write(writes)
    else:
        document_stream = grouper(batch_size, writes)
        collect = client.get_database(database).get_collection('meta')
        for chunk in document_stream:
            writes = [write for write in chunk]
            _retry_write(writes, collect.bulk_write, 5)


def _find(
        client: pymongo.MongoClient,
        collection: str,
        query: typing.Dict,
        database: typing.Optional[str] = None) -> typing.Iterable[typing.Dict]:
    return client.get_database(database)\
                 .get_collection('meta')\
                 .find({'_collection': collection, **query}, {'_id': False, '_collection': False})


class _Collection():

    def __init__(self, client: pymongo.MongoClient, name: str) -> None:
        self._name = name
        self._client = client
        try:
            self._db = client.get_database().name
        except pymongo.errors.ConfigurationError:
            self._db = 'track'

    def create_all(self, documents: typing.Iterable[typing.Dict], batch_size: typing.Optional[int] = None) -> None:
        _insert_all(self._client, self._name, documents, self._db, batch_size)

    def create(self, document: typing.Dict) -> None:
        _insert(self._client, self._name, document, self._db)

    def upsert_all(self,
                   documents: typing.Iterable[typing.Dict],
                   key_column: str,
                   batch_size: typing.Optional[int] = None
                  ) -> None:
        _upsert_all(self._client, self._name, documents, key_column, self._db, batch_size)

    def all(self) -> typing.Iterable[typing.Dict]:
        return _find(self._client, self._name, {}, self._db)

    def clear(self) -> None:
        _clear_collection(self._client, self._name, self._db)


class Connection():

    def __init__(self, connection_string: str) -> None:
        self._client = pymongo.MongoClient(connection_string)

    def __enter__(self) -> 'Connection':
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._client.close()

    @property
    def domains(self) -> _Collection:
        return _Collection(self._client, 'domains')

    @property
    def reports(self) -> _Collection:
        return _Collection(self._client, 'reports')

    @property
    def organizations(self) -> _Collection:
        return _Collection(self._client, 'organizations')

    @property
    def owners(self) -> _Collection:
        return _Collection(self._client, 'owners')

    @property
    def input_domains(self) -> _Collection:
        return _Collection(self._client, 'input_domains')

    @property
    def ciphers(self) -> _Collection:
        return _Collection(self._client, 'ciphers')

    def close(self) -> None:
        self._client.close()
