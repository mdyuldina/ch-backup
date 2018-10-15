"""
ClickHouse client.
"""

import logging
from datetime import datetime, timedelta
from urllib.parse import urljoin

from requests import HTTPError, Session

from . import crypto, docker, utils

DB_COUNT = 2
TABLE_COUNT = 2
ROWS_COUNT = 3

GET_ALL_USER_TABLES_SQL = utils.strip_query("""
    SELECT
        database,
        table,
        groupArray(name) AS columns
    FROM system.columns
    WHERE database NOT IN ('system')
    GROUP BY database, table
    ORDER BY database, table
    FORMAT JSONCompact
""")

GET_ALL_DATABASES = utils.strip_query("""
    SELECT name
    FROM system.databases
    WHERE name NOT IN ('system')
    FORMAT JSONCompact
""")

DROP_DATABASE = utils.strip_query("""
    DROP DATABASE {db_name}
""")

DROP_TABLE = utils.strip_query("""
    DROP TABLE {db_name}.{table_name}
""")

GET_TEST_TABLE_SCHEMA = utils.strip_query("""
    DESCRIBE {db_name}.{table_name}
    FORMAT JSONCompact
""")

GET_TEST_TABLE_DATA_SQL = utils.strip_query("""
    SELECT *
    FROM {db_name}.{table_name}
    ORDER BY {order_by}
    FORMAT JSONCompact
""")

TEST_TABLE_SCHEMA = utils.strip_query("""
    (date Date, datetime DateTime, int_num UInt32, str String)
    engine = MergeTree(date, int_num, 8192)
""")

GET_VERSION_SQL = utils.strip_query("""
    SELECT value
    FROM system.build_options
    WHERE name = 'VERSION_DESCRIBE'
""")


class ClickhouseClient:
    """
    ClickHouse Client.
    """

    def __init__(self, context, node_name):
        protocol = 'http'

        host, port = docker.get_exposed_port(
            docker.get_container(context, node_name),
            context.conf['projects']['clickhouse']['expose'][protocol])

        self._session = Session()
        self._url = '{0}://{1}:{2}'.format(protocol, host, port)
        self._timeout = 5

    def ping(self):
        """
        Ping ClickHouse server.
        """
        self._query('GET', url='ping')

    def execute(self, query):
        """
        Execute arbitrary query.
        """
        self._query('POST', query=query)

    def get_version(self):
        """
        Get ClickHouse version.
        """
        return self._query('GET', GET_VERSION_SQL)

    @staticmethod
    def get_test_db_name(db_num):
        """
        Get test database name
        """
        return 'test_db_{db_num:02d}'.format(db_num=db_num)

    @staticmethod
    def get_test_table_name(table_num):
        """
        Get test table name
        """
        return 'test_table_{table_num:02d}'.format(table_num=table_num)

    def init_schema(self):
        """
        Create test schema.
        """
        for db_num in range(1, DB_COUNT + 1):
            db_name = 'test_db_{db_num:02d}'.format(db_num=db_num)
            self._query(
                'POST', 'CREATE DATABASE IF NOT EXISTS {db_name}'.format(
                    db_name=db_name))
            for table_num in range(1, TABLE_COUNT + 1):
                table_name = 'test_table_{table_num:02d}'. \
                    format(table_num=table_num)
                self._query(
                    'POST', 'CREATE TABLE IF NOT EXISTS '
                    '{db_name}.{table_name} {table_schema}'.format(
                        db_name=db_name,
                        table_name=table_name,
                        table_schema=TEST_TABLE_SCHEMA))

    def init_data(self, mark=None):
        """
        Fill test schema with data
        """
        if mark is None:
            mark = ''
        for db_num in range(1, DB_COUNT + 1):
            db_name = self.get_test_db_name(db_num)
            for table_num in range(1, TABLE_COUNT + 1):
                rows = []
                table_name = self.get_test_table_name(table_num)
                for row_num in range(1, ROWS_COUNT + 1):
                    rows.append(', '.join(
                        self._gen_record(row_num=row_num, str_prefix=mark)))

                self._query(
                    'POST',
                    'INSERT INTO {db_name}.{table_name} FORMAT CSV'.format(
                        db_name=db_name, table_name=table_name),
                    data='\n'.join(rows))

    def get_all_user_data(self):
        """
        Retrieve all user data.
        """
        dbs_tables = self._query('GET', GET_ALL_USER_TABLES_SQL)['data']
        user_data = {}
        rows_count = 0
        for db_name, table_name, columns in dbs_tables:
            query_sql = GET_TEST_TABLE_DATA_SQL.format(
                db_name=db_name,
                table_name=table_name,
                order_by=','.join(columns))
            table_data = self._query('GET', query_sql)
            user_data['.'.join([db_name, table_name])] = table_data['data']
            rows_count += table_data['rows']
        return rows_count, user_data

    def get_all_user_schemas(self):
        """
        Retrieve DDL for user schemas.
        """
        dbs_tables = self._query('GET', GET_ALL_USER_TABLES_SQL)['data']
        all_tables_desc = {}
        for db_name, table_name, _ in dbs_tables:
            query_sql = GET_TEST_TABLE_SCHEMA.format(
                db_name=db_name, table_name=table_name)
            table_data = self._query('GET', query_sql)
            all_tables_desc[(db_name, table_name)] = table_data['data']
        return all_tables_desc

    def get_all_user_databases(self):
        """
        Get user databases.
        """
        all_dbs = self._query('GET', GET_ALL_DATABASES)['data']
        return [r[0] for r in all_dbs]

    def drop_database(self, db_name):
        """
        Drop database.
        """
        self._query('POST', DROP_DATABASE.format(db_name=db_name))

    def drop_test_table(self, db_num, table_num):
        """
        Drop test table.
        """
        self._query(
            'POST',
            DROP_TABLE.format(
                db_name=self.get_test_db_name(db_num),
                table_name=self.get_test_table_name(table_num)))

    @staticmethod
    def _gen_record(row_num=0, day_diff=None, str_len=5, str_prefix=None):
        """
        Generate test record.
        """
        if day_diff is None:
            day_diff = {'days': 0}
        if str_prefix is None:
            str_prefix = ''
        else:
            str_prefix = '{prefix}_'.format(prefix=str_prefix)

        rand_str = crypto.generate_random_string(str_len)

        dt_now = datetime.utcnow() - timedelta(**day_diff)
        row = (dt_now.strftime('%Y-%m-%d'),
               dt_now.strftime('%Y-%m-%d %H:%M:%S'), str(row_num),
               '{prefix}{rand_str}'.format(
                   prefix=str_prefix, rand_str=rand_str))

        return row

    def _query(self, method, query=None, url=None, data=None):
        if url:
            url = urljoin(self._url, url)
        else:
            url = self._url

        params = {}
        if query:
            params['query'] = query

        try:
            logging.debug('Executing ClickHouse query: %s', query)
            response = self._session.request(
                method,
                url,
                params={
                    'query': query,
                },
                data=data,
                timeout=self._timeout)

            response.raise_for_status()
        except HTTPError as e:
            logging.critical('Error while performing request: %s',
                             e.response.text)
            raise

        try:
            return response.json()
        except ValueError:
            return str.strip(response.text)
