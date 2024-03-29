# *****************************************************************************
# Â© Copyright IBM Corp. 2018.  All Rights Reserved.
#
# This program and the accompanying materials
# are made available under the terms of the Apache V2.0
# which accompanies this distribution, and is available at
# http://www.apache.org/licenses/LICENSE-2.0
#
# *****************************************************************************

import os
import datetime as dt
import logging
import urllib3
import json
import inspect
import sys
import gzip

import pandas as pd
import subprocess
from pandas.api.types import is_string_dtype, is_numeric_dtype, is_bool_dtype, is_datetime64_any_dtype, is_dict_like
from sqlalchemy import Table, Column, Integer, SmallInteger, String, DateTime, MetaData, Boolean, ForeignKey, create_engine, Float, func, and_, or_ 
from sqlalchemy.sql.sqltypes import TIMESTAMP,VARCHAR, BOOLEAN, NullType
from sqlalchemy.sql import select
from sqlalchemy.orm.session import sessionmaker
from sqlalchemy.exc import NoSuchTableError
from .util import CosClient, resample
from . import metadata as md
from . import pipeline as pp
from .enginelog import EngineLogging

logger = logging.getLogger(__name__)
DB2_INSTALLED = True
try:
    from ibm_db_sa.base import DOUBLE
    import ibm_db
    import ibm_db_dbi
except ImportError:
    DB2_INSTALLED = False
    msg = 'IBM_DB is not installed. Reverting to sqlite for local development with limited functionality'
    logger.warning(msg)

class Database(object):
    '''
    Use Database objects to establish database connectivity, manage database metadata and sessions, build queries and write DataFrames to tables.
    
    Parameters:
    -----------
    credentials: dict (optional)
        Database credentials. If none specified use DB_CONNECTION_STRING environment variable
    start_session: bool
        Start a session when establishing connection
    echo: bool
        Output sql to log
    '''
    
    system_package_url = 'git+https://github.com/rweidinger/functions.git@'
    
    def __init__(self,credentials = None, start_session = False, echo = False, tenant_id = None):
        
        self.function_catalog = {} #metadata for functions in catalog
        self.write_chunk_size = 1000
        self.credentials = {}
        if credentials is None:
            credentials = {}
            
        #  build credentials dictionary from multiple versions of input
        #  credentials and/or environment variables
            
        try:
            self.credentials['objectStorage'] = credentials['objectStorage']
        except (TypeError,KeyError):
            self.credentials['objectStorage'] = {}
            try:
                self.credentials['objectStorage']['region'] = os.environ.get('COS_REGION')
                self.credentials['objectStorage']['username'] = os.environ.get('COS_HMAC_ACCESS_KEY_ID')
                self.credentials['objectStorage']['password'] = os.environ.get('COS_HMAC_SECRET_ACCESS_KEY')
            except KeyError:
                msg = ('No objectStorage credentials supplied and COS_REGION,' 
                       ' COS_HMAC_ACCESS_KEY_ID, COS_HMAC_SECRET_ACCESS_KEY,' 
                       ' COS_ENDPOINT not set. COS not available. Will write'
                       ' to filesystem instead' )
                logger.warning(msg)
                self.credentials['objectStorage']['path'] = ''
            
        #tenant_id
            
        if tenant_id is None:
            tenant_id = credentials.get('tenantId',
                            credentials.get('tenant_id',
                                credentials.get('tennantId',
                                    credentials.get('tennant_id',None))))
            
        self.credentials['tenant_id'] = tenant_id
        if self.credentials['tenant_id'] is None:
            raise RuntimeError(('No tenant id supplied in credentials or as arg.'
                                ' Please supply a valid tenant id.'))
        
        # iotp and as
        try:
            self.credentials['iotp']= credentials['iotp']
        except (KeyError,TypeError):
            self.credentials['iotp'] = None
        try:
            self.credentials['db2']= credentials['db2']
        except (KeyError,TypeError):
            try:
                credentials['host']
            except (KeyError,TypeError):
                pass
            else:
                db2_creds={}
                db2_creds['host'] = credentials['host']
                db2_creds['hostname'] = credentials['host']
                db2_creds['password'] = credentials['password']
                db2_creds['port'] = credentials['port']
                db2_creds['db'] = credentials['db']
                db2_creds['databaseName'] = credentials['database']
                db2_creds['username'] = credentials['username']
                self.credentials['db2']= db2_creds
                logger.warning('Old style credentials still work just fine, but will be depreciated in the future. Check the usage section of the UI for the updated credentials dictionary')
                self.credentials['as']= credentials
        else:
            try:
                self.credentials['db2']['databaseName'] = self.credentials['db2']['database']
            except KeyError:
                pass
        
        self.credentials['message_hub'] =credentials.get('messageHub',None)
        if self.credentials['message_hub'] is None:
            msg = ('Unable to locate message_hub credentials.'
                   ' Database object created, but it will not be able interact'
                   ' with message hub.')
            logger.debug(msg)
        try:
            self.credentials['config']= credentials['config']
        except (KeyError,TypeError):
            self.credentials['config'] = {}
            self.credentials['config']['objectStorageEndpoint'] = os.environ.get('COS_ENDPOINT')
            self.credentials['config']['bos_runtime_bucket'] = os.environ.get('COS_BUCKET_KPI')

        try:
            self.credentials['objectStorage']['region']
            self.credentials['objectStorage']['username']
            self.credentials['objectStorage']['password']
            self.credentials['config']['objectStorageEndpoint']
            self.credentials['config']['bos_runtime_bucket']
        except KeyError:
            msg = 'Missing objectStorage credentials. Database object created, but it will not be able interact with object storage'
            logger.warning(msg)
        
        as_creds = credentials.get('iotp',None)
        if as_creds is None:
            as_api_host = credentials.get('as_api_host',None)
            as_api_key = credentials.get('as_api_key',None)
            as_api_token = credentials.get('as_api_token',None)
        else:
            as_api_host = as_creds.get('asHost',None)
            as_api_key = as_creds.get('apiKey',None)
            as_api_token = as_creds.get('apiToken',None)
        
        try:
            if as_api_host is None:
                as_api_host = os.environ.get('API_BASEURL')
            if as_api_key is None:
                as_api_key = os.environ.get('API_KEY')
            if as_api_token is None:
                as_api_token = os.environ.get('API_TOKEN')
        except KeyError:
            as_api_host = None
            as_api_key = None
            as_api_token = None
            msg = 'Unable to locate AS credentials or environment variable. db will not be able to connect to the AS API'
            logger.warning(msg)
               
        if as_api_host is not None and as_api_host.startswith('https://'):
            as_api_host = as_api_host[8:]

        self.credentials['as'] = {
                'host' : as_api_host,
                'api_key' : as_api_key,
                'api_token' : as_api_token                
                }

        self.tenant_id = self.credentials['tenant_id']
        
        if DB2_INSTALLED:
            connection_kwargs = {
                            'pool_size' : 1
                             }
            
            # sqlite is not included included in the AS credentials. It is only intended to be used if db2 is not istalled.
            # There is a back door to for using it instead of db2 for local development only. 
            # It will be used only when explicitly added to the credentials as credentials['sqlite'] = filename
            try:
                connection_string = 'sqlite:///%s' %(credentials['sqlite'])
            except (KeyError,TypeError):
                try:        
                    connection_string = 'db2+ibm_db://%s:%s@%s:%s/%s;' %(self.credentials['db2']['username'],
                                                                     self.credentials['db2']['password'],
                                                                     self.credentials['db2']['host'],
                                                                     self.credentials['db2']['port'],
                                                                     self.credentials['db2']['databaseName'])
                    if 'security' in self.credentials['db2']:
                        connection_string += 'SECURITY=%s' % self.credentials['db2']['security']
                except KeyError:
                    # look for environment variable for the ICS DB2
                    try:
                        msg = 'Function requires a database connection but one could not be established. Pass appropriate db_credentials or ensure that the DB_CONNECTION_STRING is set'
                        connection_string = os.environ.get('DB_CONNECTION_STRING')
                    except KeyError:
                        raise ValueError(msg)
                    else:
                        if not connection_string is None:
                            if connection_string.endswith(';'):
                                connection_string = connection_string[:-1]
                            ev = dict(item.split("=") for item in connection_string.split(";"))
                            connection_string  = 'db2+ibm_db://%s:%s@%s:%s/%s;' %(ev['UID'],ev['PWD'],ev['HOSTNAME'],ev['PORT'],ev['DATABASE'])
                            if 'SECURITY' in ev:
                                connection_string += 'SECURITY=%s' % ev['SECURITY']
                            self.credentials['db2'] =  {
                                "username": ev['UID'],
                                "password": ev['PWD'],
                                "database": ev['DATABASE'],
                                "port": ev['PORT'],
                                "host": ev['HOSTNAME'] 
                            }
                        else:
                            raise ValueError(msg)
            else:
                self.credentials['sqlite'] = connection_string
                connection_kwargs = {} 
                msg = 'Using sqlite connection for local testing. Note sqlite can only be used for local testing. It is not a supported AS database.'
                logger.warning(msg)                
                self.write_chunk_size = 100
        else:
            self.write_chunk_size = 100
            connection_string = 'sqlite:///sqldb.db'
            connection_kwargs = {}
            msg = 'Created a default sqlite database. Database file is in your working directory. Filename is sqldb.db'
            logger.info(msg)

        self.http = urllib3.PoolManager()
        try:
            self.cos_client = CosClient(self.credentials)
        except KeyError:
            msg = 'Unable to setup a cos client due to missing credentials. COS writes disabled'
            logger.warning(msg)
            self.cos_client = None
        else:
            msg = 'created a CosClient object'
            logger.debug(msg)

        EngineLogging.set_cos_client(self.cos_client)            
                
        self.connection =  create_engine(connection_string, echo = echo, **connection_kwargs)
        self.Session = sessionmaker(bind=self.connection)

        # this method should be invoked before the get Metadata()
        self.set_isolation_level(self.connection)

        if start_session:
            self.session = self.Session()
        else:
            self.session = None
        self.metadata = MetaData(self.connection)
        logger.debug('Db connection established')
            
        #cache entity types
        self.entity_type_metadata = {}
        metadata = self.http_request(object_type='allEntityTypes',
                           object_name='',
                           request = 'GET',
                           payload = {},
                           object_name_2='')
        if metadata is not None:
            try:
                metadata = json.loads(metadata)
            except:
                metadata = None
        if metadata is None:
            msg = 'Unable to retrieve entity metadata from the server. Proceeding with limited metadata'
            logger.warning(msg)
            metadata = []
        for m in metadata:
            self.entity_type_metadata[m['name']] = m
            
            
    def _aggregate_item(self,table,column_name,aggregate,alias_column=None, dimension_table = None, timestamp_col = None):
        
        if alias_column is None:
            if column_name == timestamp_col:
                if aggregate == 'min':
                    alias_column = 'first_%s' %timestamp_col
                elif aggregate == 'max':
                    alias_column = 'last_%s' %timestamp_col              
                else:
                    alias_column = '%s_%s' %(alias_column,timestamp_col)
            else:
                alias_column = column_name
            
        agg_map = {
                'count': func.count,
                'max' : func.max,
                'mean' : func.avg,
                'min' : func.min,
                'std' : func.std,
                'sum' : func.sum
                }
        
        try:
            agg_function = agg_map[aggregate]
        except KeyError:
            msg = 'Unsupported database aggregegate function %s' % aggregate
            raise ValueError (msg)           
        
        try:
            col = agg_function(table.c[column_name]).label(alias_column)
        except KeyError:
            try:
                col = agg_function(dimension_table.c[column_name]).label(alias_column)
            except (KeyError,AttributeError):
                msg = 'Aggregate column %s not present in table or on dimension' %column_name
                raise KeyError(msg)
                
        return col
    
    def _is_not_null(self, table, dimension_table, column):
        '''
        build an is not null condition for the column pointing to the table or dimension table
        '''
        
        try:
            return table.c[column].isnot(None)
        except KeyError:
            try:
                return dimension_table.c[column].isnot(None)
            except (KeyError,AttributeError):
                msg = 'Column %s not found on time series or dimension table.' %column
                raise ValueError(msg)        

    def cos_load(self, filename, bucket=None, binary=False):
        if bucket is None:
            bucket = self.credentials['config']['bos_runtime_bucket']        
        if self.cos_client is not None:
            obj = self.cos_client.cos_get(key=filename, bucket=bucket, binary=binary)
        else:
            obj = None
        if obj is None:
            logger.error('Not able to GET %s from COS bucket %s' % (filename, bucket))
        return obj
    
    def cos_save(self, persisted_object, filename, bucket=None, binary=False):
        if bucket is None:
            bucket = self.credentials['config']['bos_runtime_bucket']
        if self.cos_client is not None:
            ret = self.cos_client.cos_put(key=filename, payload=persisted_object, bucket=bucket, binary=binary)
        else:
            ret = None
        if ret is None:
            logger.info('Not able to PUT %s to COS bucket %s',filename, bucket)
        return ret

    def cos_delete(self, filename, bucket=None):
        if bucket is None:
            bucket = self.credentials['config']['bos_runtime_bucket']
        if self.cos_client is not None:
            ret = self.cos_client.cos_delete(key=filename, bucket=bucket)
        else:
            ret = None
        if ret is None:
            logger.info('Not able to DELETE %s to COS bucket %s', (filename, bucket))
        return ret

    def commit(self):
        '''
        Commit the active session
        '''
        if not self.session is None:
            self.session.commit()
            self.session.close()
            self.session = None
     
        
    def create(self,tables = None, checkfirst = True ):
        '''
        Create database tables for logical tables defined in the database metadata
        '''
        
        self.metadata.create_all(tables = tables, checkfirst = checkfirst)
        
        
    def cos_create_bucket(self, bucket=None):
        '''
        Create a bucket in cloud object storage
        '''
        
        if bucket is None:
            logger.info('Not able to CREATE the bucket. A name should be provided.')
        if self.cos_client is not None:
            ret = self.cos_client.cos_put(key=None, payload=None, bucket=bucket)
        else:
            ret = None
        if ret is None:
            logger.info('Not able to CREATE the bucket %s.'% bucket)
        return ret
    
    def delete_data(self, table_name, schema = None, timestamp = None,older_than_days = None):
        '''
        Delete data from table. Optional older_than_days parameter deletes old data only.
        '''
        try:
            table = self.get_table(table_name,schema=schema)
        except KeyError:
            msg = 'No table %s in schema %s' %(table_name,schema)
            raise KeyError(msg)
            
        self.start_session()
        if older_than_days is None:
            result = self.connection.execute(table.delete())
            msg = 'deleted all data from table %s' %table_name
            logger.debug(msg)
        else:
            until_date = dt.datetime.utcnow() - dt.timedelta(days=older_than_days)
            result = self.connection.execute(table.delete().where(table.c[timestamp]<until_date))
            msg = 'deleted data from table %s older than %s' %(table_name,until_date)
            logger.debug(msg)
        self.commit()
    
        
    def drop_table(self,table_name,schema=None):
        
        try:
            table = self.get_table(table_name,schema)
        except KeyError:
            msg = 'Didnt drop table %s because it doesnt exist in schema %s' %(table_name,schema)
        else:
            self.start_session()
            self.metadata.drop_all(tables = [table], checkfirst = True) 
            msg = 'Dropped table name %s' %table.name
            self.session.commit()
        logger.debug(msg)

        
    def execute_job(self,entity_type,schema=None,**kwargs):
        
        if isinstance(entity_type,str):
            entity_type = md.ServerEntityType(
                    logical_name = entity_type,
                    db = self,
                    db_schema = schema
                    )
        
        job = pp.JobController(payload=entity_type,**kwargs)
        job.execute()
        
    def get_as_datatype(self,column_object):
        '''
        Get the datatype of a sql alchemy column object and convert it to an
        AS server datatype string
        '''
        
        data_type = column_object.type
        
        if isinstance(data_type,DOUBLE) or isinstance(data_type,Float) or isinstance(data_type,Integer):
                data_type = 'NUMBER'
        elif isinstance(data_type,VARCHAR) or isinstance(data_type,String):
            data_type = 'LITERAL'
        elif isinstance(data_type,TIMESTAMP) or isinstance(data_type,DateTime):
            data_type = 'TIMESTAMP'
        elif isinstance(data_type,BOOLEAN) or isinstance(data_type,NullType) or isinstance(data_type,Boolean):
            data_type = 'BOOLEAN'            
        else:
            data_type = str(data_type)
            logger.warning('Unknown datatype %s for column %s' %(data_type,column_object.name))

        return data_type            
        
        
    def get_catalog_module(self,class_name):
        
        package = self.function_catalog[class_name]['package']
        module = self.function_catalog[class_name]['module']
        
        return (package,module)        
        
    def get_entity_type(self,name):
        '''
        Get an EntityType instance by name. Name may be the logical name shown in the UI or the table name.'
        
        '''
        metadata = None
        try:
            metadata = self.entity_type_metadata[name]
        except KeyError:
            for m in list(self.entity_type_metadata.values()):
                if m['metricTableName'] == name:
                    metadata = m
                    break
            msg = 'No entity called % in the cached metadata.' %name
            raise ValueError(msg)
            
        print (metadata)
        raise
                
        timestamp = metadata['metricTimestampColumn']
        schema = metadata['schemaName']
        dim_table = metadata['dimensionTableName']
        entity_type_id = metadata.get('entityTypeId', None) 

        entity = md.EntityType( name = name,
                             db = self,
                             **{
                            'auto_create_table' : False,
                            '_timestamp' : timestamp,
                            '_db_schema' : schema,
                            '_entity_type_id' : entity_type_id,
                            '_dimension_table_name' : dim_table
                             }
                             )
        
        return entity
        
        
    def get_table(self,table_name, schema = None):
        '''
        Get sql alchemchy table object for table name
        '''
        
        if isinstance(table_name,str):
            kwargs = {
                    'schema': schema
                    }
            try:
                table = Table(table_name, self.metadata, autoload=True,autoload_with=self.connection,**kwargs)        
            except NoSuchTableError:
                raise KeyError ('Table %s does not exist in the schema %s ' %(table_name,schema))
        elif issubclass(table_name.__class__,BaseTable):
            table = table_name.table
        elif isinstance(table_name,Table):
            table = table_name
        else:
            msg = 'Cannot get sql alchemcy table object for %s' %table_name
            raise ValueError(msg)
            
        return table
        
    def get_column_lists_by_type(self, table, schema = None, exclude_cols = None):
        """
        Get metrics, dates and categoricals and others
        """
        
        table = self.get_table(table, schema = schema)
        
        if exclude_cols is None:
            exclude_cols = []
        metrics = []
        dates = []
        categoricals = []
        others = []
        
        for c in self.get_column_names(table):
            if not c in exclude_cols:
                data_type = table.c[c].type
                if isinstance(data_type,DOUBLE) or isinstance(data_type,Float):
                    metrics.append(c)
                elif isinstance(data_type,VARCHAR) or isinstance(data_type,String):
                    categoricals.append(c)
                elif isinstance(data_type,TIMESTAMP) or isinstance(data_type,DateTime):
                    dates.append(c)
                else:
                    others.append(c)
                    msg = 'Found column %s of unknown data type %s' %(c,data_type.__class__.__name__)
                    logger.warning(msg)
                    
        return (metrics,dates,categoricals,others)        
        
    def get_column_names(self,table, schema=None):
        """
        Get a list of columns names for a table object or table name
        """
        if isinstance(table,str):
            table = self.get_table(table, schema)
        
        return [column.key for column in table.columns]
    
    
    def http_request(self,
                     object_type,
                     object_name,
                     request,
                     payload=None,
                     object_name_2='',
                     raise_error = False,
                     ):
        '''
        Make an api call to AS.
        
        Warning: This is a low level API that closley maps to the AS Server API.
        The AS Server API changes regularly. This API will not shield you from
        these changes. Consult the iotfunctions wiki and view samples to understand
        the supported APIs for interacting with the AS Server.
        
        
        Parameters
        ----------
        object_type : str 
            function,allFunctions, entityType, kpiFunctions
        object_name : str
            name of object
        request : str
            GET, POST, DELETE, PUT
        payload : dict
            Dictionary will be encoded as JSON
        
        '''
        if object_name is None:
            object_name = ''
        if payload is None:
            payload = ''            
        
        if self.tenant_id is None:
            msg = 'tenant_id instance variable is not set. database object was not initialized with valid credentials'
            raise ValueError(msg)
        
        base_url = 'http://%s/api' %(self.credentials['as']['host'])
        self.url = {}
        self.url[('allFunctions','GET')] = '/'.join([base_url,'catalog','v1',self.tenant_id,'function?customFunctionsOnly=false'])
        
        self.url[('constants','GET')] = '/'.join([base_url,'constants','v1','%s?entityType=%s'%(self.tenant_id,object_name)])
        self.url[('constants','PUT')] = '/'.join([base_url,'constants','v1'])
        self.url[('constants','POST')] = '/'.join([base_url,'constants','v1'])
        
        self.url[('defaultConstants','GET')] = '/'.join([base_url,'constants','v1',self.tenant_id])
        self.url[('defaultConstants','POST')] = '/'.join([base_url,'constants','v1',self.tenant_id])
        self.url[('defaultConstants','PUT')] = '/'.join([base_url,'constants','v1',self.tenant_id])
        self.url[('defaultConstants','DELETE')] = '/'.join([base_url,'constants','v1',self.tenant_id])
        
        self.url[('dataItem','PUT')] = '/'.join([base_url,'kpi','v1',self.tenant_id,'entityType',object_name,object_type,object_name_2]) 
        
        self.url[('allEntityTypes','GET')] = '/'.join([base_url,'meta','v1',self.tenant_id,'entityType'])
        self.url[('entityType','POST')] = '/'.join([base_url,'meta','v1',self.tenant_id,object_type])
        self.url[('entityType','GET')] = '/'.join([base_url,'meta','v1',self.tenant_id,object_type,object_name])
        
        self.url[('engineInput','GET')] = '/'.join([base_url,'kpi','v1',self.tenant_id,'entityType',object_name,object_type])
        
        self.url[('function','GET')] = '/'.join([base_url,'catalog','v1',self.tenant_id,object_type,object_name])
        self.url[('function','DELETE')] = '/'.join([base_url,'catalog','v1',self.tenant_id,object_type,object_name])
        self.url[('function','PUT')] = '/'.join([base_url,'catalog','v1',self.tenant_id,object_type,object_name])
        
        self.url[('granularitySet','POST')] = '/'.join([base_url,'granularity','v1',self.tenant_id,'entityType',object_name,object_type])
        self.url[('granularitySet','DELETE')] = '/'.join([base_url,'granularity','v1',self.tenant_id,'entityType',object_name,object_type,object_name_2])
        self.url[('granularitySet','GET')] = '/'.join([base_url,'granularity','v1',self.tenant_id,'entityType',object_name,object_type])

        self.url[('kpiFunctions','POST')] = '/'.join([base_url,'kpi','v1',self.tenant_id,'entityType',object_name,object_type,'import'])            

        self.url[('kpiFunction','POST')] = '/'.join([base_url,'kpi','v1',self.tenant_id,'entityType',object_name,object_type]) 
        self.url[('kpiFunction','DELETE')] = '/'.join([base_url,'kpi','v1',self.tenant_id,'entityType',object_name,object_type,object_name_2]) 
        self.url[('kpiFunction','GET')] = '/'.join([base_url,'kpi','v1',self.tenant_id,'entityType',object_name,object_type])         
        self.url[('kpiFunction','PUT')] = '/'.join([base_url,'kpi','v1',self.tenant_id,'entityType',object_name,object_type,object_name_2])

        encoded_payload = json.dumps(payload).encode('utf-8')        
        headers = {
            'Content-Type': "application/json",
            'X-api-key' : self.credentials['as']['api_key'],
            'X-api-token' : self.credentials['as']['api_token'],
            'Cache-Control': "no-cache",
        }        
        try:
            url =self.url[(object_type,request)]
        except KeyError:
            raise ValueError (('This combination  of request_type (%s) and' 
                               ' object_type (%s) is not supported by the' 
                               ' python api') %(object_type,request))            
            
        r = self.http.request(request,url, body = encoded_payload, headers=headers)
        response= r.data.decode('utf-8')
        
        if 200  <= r.status <=  299:
            logger.debug('http request successful. status %s',r.status)
        elif (request == 'POST' and
              object_type in ['kpiFunction','defaultConstants','constants'] and
              (500  <= r.status <=  599)
              ):
                logger.debug(('htpp POST failed. attempting PUT. status:%s'),
                             r.status)
                response = self.http_request(object_type = object_type,
                                        object_name = object_name,
                                        request = 'PUT',
                                        payload = payload,
                                        object_name_2 = object_name_2,
                                        raise_error = raise_error
                                        )
        elif (400  <= r.status <=  499):
            logger.debug('Http request client error. status: %s' ,r.status)
            logger.debug('url: %s', url)
            logger.debug('payload: %s', encoded_payload)                 
            logger.debug('http response: %s', r.data)
            if raise_error:
                raise urllib3.exceptions.HTTPError(r.data)
        elif (500  <= r.status <=  599):
            logger.debug('Http request server error. status: %s' ,r.status)
            logger.debug('url: %s', url)
            logger.debug('payload: %s', encoded_payload)                 
            logger.debug('http response: %s', r.data)
            if raise_error:
                raise urllib3.exceptions.HTTPError(r.data)
        else :
            logger.debug('Http request unknown error. status: %s' ,r.status)
            logger.debug('url: %s', url)
            logger.debug('payload: %s', encoded_payload)                 
            logger.debug('http response: %s', r.data)
            if raise_error:
                raise urllib3.exceptions.HTTPError(r.data)
        
        return response                
    
        
    def if_exists(self,table_name, schema=None):
        '''
        Return True if table exists in the database
        '''
        try:
            self.get_table(table_name,schema)
        except KeyError:
            return False
        
        return True
        
    def install_package(self, url):
        '''
        Install python package located at URL
        '''

        msg = 'running pip install for url %s' %url
        logger.debug(msg)

        try:
            completedProcess = subprocess.run(
                    ['pip', 'install', 
                     '--process-dependency-links',
                     '--upgrade', url],
                     stderr=subprocess.STDOUT,
                     stdout=subprocess.PIPE,
                     universal_newlines=True)
        except Exception as e:
            raise ImportError('pip install for url %s failed: \n%s',
                           url, str(e)) 
            
        if completedProcess.returncode == 0:

            logger.debug('pip install for url %s was successful: \n %s',
                         url, completedProcess.stdout)
            
        else:

            raise ImportError('pip install for url %s failed: \n %s.',
                           url, completedProcess.stdout)
        
         
    def import_target(self,package,module,target,url=None):
        '''
        from package.module import target
        if a url is specified import missing packages
        '''
        
        if module is not None:
            impstr = 'from %s.%s import %s' %(package,module,target)
        else:
            impstr = 'from %s import %s' %(package,target)
        logger.debug(impstr)
        try:
            exec(impstr)
        except BaseException:
            if url is not None:
                try:
                    self.install_package(url)
                except ImportError:
                    return (None,'package_error')  
                else:
                    return self.import_target(package=package,module=module,target=target)
            else:
                return (None,'package_error')
        except ImportError:
            return (None,'target_error')
        else:
            return (target,'ok')
    
    def load_catalog(self,install_missing=True,
                     unregister_invalid_target=False,
                     function_list = None):
        '''
        Import all functions from the AS function catalog.
        
        Returns: 
        --------
        dict keyed on function name
        '''
        
        imported = {}
        result = {}

        fns = json.loads(self.http_request('allFunctions',
                                           object_name = None,
                                           request = 'GET',
                                           payload = None))
        for fn in fns:
            path = fn["moduleAndTargetName"].split('.')
            name = fn["moduleAndTargetName"]
            if path is None:
                msg = 'Cannot import %s it has an invalid module and path %s' %(name,path)
                logger.warning(msg)
                tobj = None
                status = 'metadata_error'
            else:
                (package,module,target) = (path[0],path[1],path[2])
                if (function_list is not None) and (target not in function_list):
                    continue                
                if install_missing:
                    url = fn['url']
                else:
                    url = None
                try:
                    tobj,status = self.import_target(package=package,
                                                     module=module,
                                                     target=target,
                                                     url=url)    
                except Exception as e:
                    msg = 'unkown error when importing: %s' %name
                    logger.exception(msg)
                    raise e
            try:
                (epackage,emodule) = imported[target]
            except KeyError:

                result[target] = {
                        'package':package,
                        'module':module,
                        'status':status,
                        'meta' :fn
                        }
                imported[target] = (package,module)
            else:
                if (package,module) != (epackage,emodule):
                    logger.warning(
                        ('Duplicate class name encountered on import of'
                         ' %s. Ignored %s.%s'),name,package,module)
                    
            if status == 'target_error' and unregister_invalid_target:
                self.unregister_functions([name])
                msg = 'Unregistered invalid function %s' %name
                logger.info(msg)
        
        logger.debug('Imported %s functions from catalog',len(imported))
        self.function_catalog = result
                
        return result


    def make_function(self,function_name, function_code,
                      filename=None, bucket =None):
        
        exec (function_code)
        
        fn = locals()[function_name]
        if filename is not None and bucket is not None:
            self.cos_save(fn,
                          filename = filename,
                          bucket = bucket,
                          binary = True)
            
        return fn
    
    
    
    def subquery_join(self,left_query,right_query,*args,**kwargs):
        '''
        Perform an equijoin between two sql alchemy query objects, filtering the left query by the keys in the right query
        args are the names of the keys to join on, e.g 'deviceid', 'timestamp'. 
        Use string args for joins on common names. Use tuples like ('timestamp','evt_timestamp') for joins on different column names.
        By default the join acts as a filter. It does not return columns from the right query. To return columns from the right
        query specify **kwargs as a dict containing column names and alais names.
        '''
        left_query = left_query.subquery('a')
        right_query = right_query.subquery('b')
        joins = []
        for col in args:
            if isinstance(col,str):
                joins.append(left_query.c[col]==right_query.c[col])
            else:
                joins.append(left_query.c[col[0]]==right_query.c[col[1]])
        projection_list = []
        covered_columns = set()
        for (col,alias) in list(kwargs.items()):
            projection_list.append(right_query.c[col].label(alias))
            covered_columns.add(alias)
        #add left hand cols to project list if not already added from right
        for col_obj in list(left_query.c.values()):
            if col_obj.name not in covered_columns:
                projection_list.append(col_obj)
        join_condition = and_(*joins)
        join = left_query.join(right_query, join_condition)
        result_query = select(projection_list).select_from(join)
        return result_query

    def set_isolation_level(self, conn):
        if DB2_INSTALLED:
            with conn.connect() as con:
                con.execute('SET ISOLATION TO DIRTY READ;')  #specific for DB2
    
    
    def get_query_data(self, query):
        '''
        Execute a query and a return a dataframe containing results
        
        Parameters
        ----------
        query : sqlalchemy Query object
            query to execute
        
        '''
        
        df = pd.read_sql(sql=query.statement, con = self.connection )
        return df
        
        
    def start_session(self):
        '''
        Start a database session. 
        '''
        if self.session is None:
            self.session = self.Session()
            
    def truncate(self,table_name,schema=None):
        
        try:
            table = self.get_table(table_name,schema)
        except KeyError:
            msg = 'Table %s doesnt exist in the the database' %table_name
            raise KeyError(msg)
        else:
            self.start_session()
            table.delete()
            self.commit()
            msg = 'Truncated table name %s' %table_name
        logger.debug(msg)      
        
        
    def read_table(self,table_name,
                   schema,
                   parse_dates = None,
                   columns = None,
                   timestamp_col = None,
                   start_ts = None,
                   end_ts = None,
                   entities = None,
                   dimension = None
                   ):
        '''
        Read whole table and return as dataframe
        
        Parameters
        -----------
        table_name: str
            Source table name
        schema: str
            Schema name where table is located
        columns: list of strs
            Projection list
        timestamp_col: str
            Name of timestamp column in the table. Required for time filters.
        start_ts: datetime
            Retrieve data from this date
        end_ts: datetime
            Retrieve data up until date
        entities: list of strs
            Retrieve data for a list of deviceids
        dimension: str
            Table name for dimension table. Dimension table will be joined on deviceid. 
        parse_dates: list of strs
            Column names to parse as dates
        
        
        '''
        q,table = self.query(table_name,
                             schema=schema,
                             column_names = columns,
                             timestamp_col = timestamp_col,
                             start_ts = start_ts,
                             end_ts = end_ts,
                             entities = entities,
                             dimension = dimension)
        df = pd.read_sql(sql=q.statement,con=self.connection,parse_dates=parse_dates,columns=columns)
        return(df)
        
    def read_sql(self,sql,parse_dates =None,columns=None):
        '''
        Read whole table and return as dataframe
        '''
        df = pd.read_sql(sql,con=self.connection,parse_dates=parse_dates,columns=columns)
        return(df)

    def read_query(self,query,parse_dates =None,columns=None):
        '''
        Read whole table and return as dataframe
        '''
        
        try:
            query = query.statement
        except AttributeError:
            pass
        
        df = pd.read_sql(query,con=self.connection,parse_dates=parse_dates,columns=columns)
        return(df)
        
    def read_agg(self, table_name, schema, agg_dict,
                       agg_outputs = None,
                       groupby=None,
                       timestamp=None,
                       time_grain = None,
                       dimension = None,
                       start_ts = None,
                       end_ts = None,
                       entities = None):
        '''
        Pandas style aggregate function against db table
        
        Parameters
        ----------
        table_name: str
            Source table name
        schema: str
            Schema name where table is located
        agg_dict: dict
            Dictionary of aggregate functions keyed on column name, e.g. { "temp": "mean", "pressure":["min","max"]}
        timestamp: str
            Name of timestamp column in the table. Required for time filters.
        time_grain: str
            Time grain for aggregation may be day,month,year or a pandas frequency string
        start_ts: datetime
            Retrieve data from this date
        end_ts: datetime
            Retrieve data up until date
        entities: list of strs
            Retrieve data for a list of deviceids
        dimension: str
            Table name for dimension table. Dimension table will be joined on deviceid.
        '''
        
        (query,table,dim,pandas_aggregate,agg_dict) = self.query_agg(
                    agg_dict = agg_dict,
                    agg_outputs = agg_outputs,
                    table_name = table_name,
                    schema = schema,
                    groupby = groupby,
                    timestamp = timestamp,
                    time_grain = time_grain,
                    dimension = dimension,
                    start_ts = start_ts,
                    end_ts = end_ts,
                    entities = entities
                )

        sql = query.statement.compile(compile_kwargs={"literal_binds": True})
        df = pd.read_sql(sql,con = self.connection)
        logger.debug(sql)
        if pandas_aggregate is not None:
            df = resample(df=df,time_frequency=pandas_aggregate,timestamp=timestamp,dimensions=groupby,agg=agg_dict)
        return df
    
    def register_constants(self,constants, raise_error = True):
        '''
        Register one or more server properties that can be used as entity type 
        properties in the AS UI
        
        Constants are UI objects.
        '''
        
        if not isinstance(constants,list):
            constants = [constants]
        payload = []
        for c in constants:
            meta = c.to_metadata()
            name = meta['name']
            default = meta.get('value',None)
            del meta['name']
            try:
                del meta['value']
            except KeyError:
                pass
            payload.append( {'name' : name,
                       'entityType' : None,
                       'enabled' : True,
                       'value' : default,
                       'metadata': meta})
        self.http_request(object_type='defaultConstants',
                          object_name=None,
                          request = "POST",
                          payload=payload,
                          raise_error = True)
            

    def register_functions(self,functions,
                           url=None,
                           raise_error = True,
                           force_preinstall=False):
        '''
        Register one or more class for use with AS
        '''
        
        if not isinstance(functions,list):
            functions = [functions]
            
        for f in functions:
            
            if url is None:
                url = f.url
            
            if isinstance(f,type):
                name = f.__name__
            else:
                name = f.__class__.__name__
                
            try:
                is_deprecated = f.is_deprecated
            except AttributeError:
                is_deprecated = False
            if is_deprecated:
                logger.warning('Registering deprecated function %s', name)
            
            module = f.__module__
            module_obj = sys.modules[module]
            
            
            # the _IS_PREINSTALLED module variable is reserved for 
            # AS system functions
            try:
                is_preinstalled = getattr(module_obj,'_IS_PREINSTALLED')
            except AttributeError:
                is_preinstalled = False
                
            logger.debug('%s is preinstalled %s',module_obj,is_preinstalled)
                
            if is_preinstalled:
                if force_preinstall :
                    if url != self.system_package_url:
                        msg = ('Cannot register function %s. This '
                         ' module has _IS_PREINSTALLED = True'
                         ' but its catalog source is not the'
                         ' iotfunctions catalog url' %name)
                        if raise_error:
                            raise RuntimeError(msg)
                        else:
                            logger.debug(msg)
                            continue
                    else:
                        # URL should not be set for preinstalled functions
                        url = None
                        logger.debug(('Registering preinstalled function %s with'
                                      ' url %s') , name,url)
                else:
                    msg = ('Cannot register function %s. This is a'
                               ' preinstalled function' %name )
                    logger.debug(msg)
                    continue
            
            if module == '__main__':
                raise RuntimeError('The function that you are attempting to register is not located in a package. It is located in __main__. Relocate it to an appropriate package module.')

            module_and_target = '%s.%s' %(module,name)
            exec_str = 'from %s import %s as import_test' %(module,name)
            try:
                exec (exec_str)
            except ImportError:
                raise ValueError(
                    ('Unable to register function as local import failed.'
                     ' Make sure it is installed locally and '
                     ' importable. %s ' %exec_str) )

            try:
                category = f.category
            except AttributeError:
                category = 'TRANSFORMER'            
            try:
                tags = f.tags
            except AttributeError:
                tags = None  
            try:
                (metadata_input,metadata_output) = f.build_ui()
                (input_list,output_list) = f._transform_metadata(metadata_input,metadata_output)
            except (AttributeError,NotImplementedError):
                msg = 'Function %s has no build_ui method. It cannot be registered this way. Register using function_instance.register()' %name
                raise NotImplementedError (msg)                
            payload = {
                'name': name,
                'description': f.__doc__,
                'category': category,
                'moduleAndTargetName': module_and_target,
                'url': url,
                'input': input_list,
                'output':output_list,
                'incremental_update': True if category == 'AGGREGATOR' else None,
                'tags' : tags
             }            
            self.http_request(object_type='function',
                              object_name=name,
                              request="DELETE",
                              payload=payload,
                              raise_error = False)
            self.http_request(object_type='function',
                              object_name=name,
                              request = "PUT",
                              payload=payload,
                              raise_error = raise_error)

    def register_module(self,module,url=None,raise_error=True,force_preinstall=False):
        '''
        Register all of the functions contained within a python module
        '''
        
        registered = set()
        for name, cls in inspect.getmembers(module):
            if inspect.isclass(cls) and cls not in registered:
                try:
                    is_deprecated = cls.is_deprecated
                except AttributeError:
                    is_deprecated = False
                if not is_deprecated and cls.__module__ == module.__name__:
                    try:
                        self.register_functions(cls,
                                                raise_error = True,
                                                url = url,
                                                force_preinstall = force_preinstall)
                    except (AttributeError,NotImplementedError):
                        msg = 'Did not register %s as it is not a registerable function' %name
                        logger.debug(msg)
                        continue
                    except BaseException as e:
                        if raise_error:
                            raise
                        else:
                            logger.debug('Error registering function: %s',str(e))
                    else:
                        registered.add(cls)
                        
        return registered
                      
    def _ts_col_rounded_to_minutes(self,table_name,schema,column_name,minutes,label):
        '''
        Returns a column expression that rounds the timestamp to the specified number of minutes
        '''
        a = self.get_table(table_name,schema)
        col = a.c[column_name]
        hour = func.add_hours(func.timestamp(func.date(col)),func.hour(col))
        min_col = (func.minute(col)/minutes)*minutes
        exp = (func.add_minutes(hour,min_col)).label(label)
        return exp
    
    
    def _ts_col_rounded_to_hours(self,table_name,schema,column_name,hours,label):
        '''
        Returns a column expression that rounds the timestamp to the specified number of minutes
        '''
        a = self.get_table(table_name,schema)
        col = a.c[column_name]
        date_col = func.timestamp(func.date(col))
        hour_col = (func.hour(col)/hours)*hours
        exp = (func.add_hours(date_col,hour_col)).label(label)
        return exp
    
    
    def query(self,table_name, schema,
              column_names = None,
              timestamp_col = None,
              start_ts = None,
              end_ts = None,
              entities = None,
              dimension = None
              ):
        '''
        Build a sqlalchemy query object for a table. You can further manipulate the query object using standard sqlalchemcy operations to do things like filter and join.
        
        Parameters
        ----------
        table_name : str or Table object
        columns_names: list of strs
            Projection list
        timestamp_col: str
            Name of timestamp column in the table. Required for time filters.
        start_ts: datetime
            Retrieve data from this date
        end_ts: datetime
            Retrieve data up until date
        entities: list of strs
            Retrieve data for a list of deviceids
        dimension: str
            Table name for dimension table. Dimension table will be joined on deviceid.
        
        Returns
        -------
        tuple containing a sqlalchemy query object and a sqlalchemy table object
        '''        
        
        self.start_session()
        table = self.get_table(table_name,schema)
        dim = None
        if dimension is not None:
            dim = self.get_table(table_name=dimension,schema=schema)
        
        if column_names is None:
            if dim is None:
                query_args = [table]
            else:
                query_args = [table]
                for col_name,col in list(dim.c.items()):
                    if col_name != 'deviceid':
                        query_args.append(col)
        else:
            query_args = []
            if isinstance(column_names,str):
                column_names = [column_names]
            for c in column_names:
                try:
                    query_args.append(table.c[c])
                except KeyError:
                    try:
                       query_args.append(dim.c[c])     
                    except KeyError:
                        msg = 'Unable to find column %s in table or dimension for entity type %s' %(c,table_name)
                        raise KeyError(msg)
    
        query = self.session.query(*query_args)
        
        if dim is not None:
            query = query.join(dim, dim.c.deviceid == table.c.deviceid)
        
        if not start_ts is None:
            if timestamp_col is None:
                msg = 'No timestamp_col provided to query. Must provide a timestamp column if you have a date filter'
                raise ValueError(msg)
            query = query.filter(table.c[timestamp_col] >= start_ts)            
        if not end_ts is None:
            if timestamp_col is None:
                msg = 'No timestamp_col provided to query. Must provide a timestamp column if you have a date filter'
                raise ValueError(msg)            
            query = query.filter(table.c[timestamp_col] < end_ts)  
        if not entities is None:
            query = query.filter(table.c.deviceid.in_(entities))
        
        return (query,table)
    
    
    def query_agg(self, table_name, schema, agg_dict,
                       agg_outputs = None,
                       groupby=None,
                       timestamp=None,
                       time_grain = None,
                       dimension = None,
                       start_ts = None,
                       end_ts = None,
                       entities = None):
        '''
        Pandas style aggregate function against db table
        
        Parameters
        ----------
        table_name: str
            Source table name
        schema: str
            Schema name where table is located
        agg_dict: dict
            Dictionary of aggregate functions keyed on column name, e.g. { "temp": "mean", "pressure":["min","max"]}
        timestamp: str
            Name of timestamp column in the table. Required for time filters.
        time_grain: str
            Time grain for aggregation may be day,month,year or a pandas frequency string
        start_ts: datetime
            Retrieve data from this date
        end_ts: datetime
            Retrieve data up until date
        entities: list of strs
            Retrieve data for a list of deviceids
        dimension: str
            Table name for dimension table. Dimension table will be joined on deviceid.
        '''        
        
        table = self.get_table(table_name,schema)
        dim = None
        if dimension is not None:
            dim = self.get_table(table_name=dimension,schema=schema)
        # assemble list as a set of aggregates to project 
        
        if isinstance(groupby,str):
            groupby = [groupby]
        
        args = []
        metric_filter = []
        # aggregate dict is keyed on column - may contain a single aggregate function or a list of aggregation functions
        for col,aggs in agg_dict.items():
            if isinstance(aggs,str):
                args.append(self._aggregate_item(table=table,column_name=col,aggregate=aggs,alias_column=None, dimension_table = dim, timestamp_col = timestamp))
            elif isinstance(aggs,list):
                for i,agg in enumerate(aggs):
                    try:
                        output = agg_outputs[col][i]
                    except (KeyError,IndexError):
                        output = '%s_%s' %(col,agg)
                        msg = 'No output item name specified for %s, %s. Using default.' %(col,agg)
                        logger.warning(msg)
                    else:
                        pass
                    args.append(self._aggregate_item(table=table,column_name=col,aggregate=agg,alias_column=output,dimension_table = dim, timestamp_col = timestamp))
            else:
                msg = 'Aggregate dictionary is not in the correct form. Supply a single aggregate function as a string or a list of strings.'
                raise ValueError(msg)
            metric_filter.append(self._is_not_null(table=table, dimension_table = dim ,column = col))
        #assemble group by
        grp = []
        #attempt to push aggregates down to sql
        #for db aggregates that can't be pushed, do them in pandas
        pandas_aggregate = None
        if time_grain is not None:
            if timestamp is None:
                msg = 'You must supply a timestamp column when doing a time-based aggregate'
                raise ValueError (msg)
            if time_grain == timestamp:
                grp.append(table.c[timestamp].label(timestamp)) 
            elif time_grain.endswith('min'):
                minutes = int(time_grain[:-3])
                grp.append(self._ts_col_rounded_to_minutes(table_name,schema,timestamp,minutes,timestamp)) 
            elif time_grain.endswith('H'):
                hours = int(time_grain[:-1])
                grp.append(self._ts_col_rounded_to_hours(table_name,schema,timestamp,hours,timestamp))                 
            elif time_grain == 'day':
                grp.append(func.day(table.c[timestamp]).label(timestamp)) 
            elif time_grain == 'week':
                grp.append(func.this_week(table.c[timestamp]).label(timestamp)) 
            elif time_grain == 'month':
                grp.append(func.this_month(table.c[timestamp]).label(timestamp)) 
            elif time_grain == 'year':
                grp.append(func.this_year(table.c[timestamp]).label(timestamp))
            else:
                pandas_aggregate = time_grain
        if groupby is None:
            groupby = []
        for g in groupby:
            try:
                grp.append(table.c[g])
            except KeyError:
                if dimension is not None:
                    try:
                        grp.append(dim.c[g])
                    except KeyError:
                        msg = 'group by column %s not found in main table or dimension table' %g  
                        raise ValueError(msg)
                else:
                    msg = 'group by column %s not found in main table and no dimension table specified' %g  
                    raise KeyError(msg)
        args.extend(grp)

        self.start_session()
        if pandas_aggregate is None:
            query = self.session.query(*args).group_by(*grp)
            if dimension is not None:
                query = query.join(dim, dim.c.deviceid == table.c.deviceid)
        else:
            (query,table) = self.query(
                        table_name = table_name,
                        schema = schema,
                        timestamp_col = timestamp,
                        start_ts = start_ts,
                        end_ts = end_ts,
                        entities = entities,
                        dimension = dimension
                    )
        #filter out rows where all of the metrics are null
        #reduces volumes when dealing with sparse datasets
        #also essential when doing a query to get the first or last values as null values must be ignored
        query = query.filter(or_(*metric_filter))
            
        return (query,table,dim,pandas_aggregate,agg_dict)
    
    
    def query_column_aggregate(self, table_name, schema, column, aggregate,
                       start_ts = None,
                       end_ts = None,
                       entities = None):

        '''
        Perform a single aggregate operation against a table to return a scalar value
        
        Parameters
        ----------
        table_name: str
            Source table name
        schema: str
            Schema name where table is located
        column: str
            column name
        aggregate: str
            aggregate function
        start_ts: datetime
            Retrieve data from this date
        end_ts: datetime
            Retrieve data up until date
        entities: list of strs
            Retrieve data for a list of deviceids
        '''         
        
        agg_dict = { column: aggregate}
                
        (query,table,dim,pandas_aggregate,agg_dict) = self.query_agg(
                    agg_dict = agg_dict,
                    table_name = table_name,
                    schema = schema,
                    groupby = None,
                    time_grain = None,
                    dimension = None,
                    start_ts = start_ts,
                    end_ts = end_ts,
                    entities = entities
                )
        
        return (query,table)
    
    def query_time_agg(self,
               table_name,
               schema,
               column,
               regular_agg,
               time_agg,
               groupby = None,
               timestamp = None,
               time_grain = None,
               dimension = None,
               start_ts = None,
               end_ts = None,
               entities = None,
               output_item = None):
        '''
        Build a query with separate aggregation functions for regular rollup and timestate rollup.
        '''
        
        if isinstance(groupby,str):
            groupby = [groupby]
        
        agg_dict = { column: regular_agg }
        
        #build query a aggregated on the regular dimension 
        (query_a,table,dim,pandas_aggregate,agg_dict) = self.query_agg(
                    agg_dict = agg_dict,
                    table_name = table_name,
                    schema = schema,
                    groupby = groupby,
                    time_grain = timestamp,
                    timestamp = timestamp,
                    dimension = dimension,
                    start_ts = start_ts,
                    end_ts = end_ts,
                    entities = entities
                )
        
        if pandas_aggregate:
            raise ValueError('Attempting to db time aggregation on a query cannot be pushed to the database. Perform the time aggregation in Pandas.')
        
        if time_agg == 'first':
            time_agg_dict =  { timestamp: "min" }
        elif time_agg == 'last':
            time_agg_dict =  { timestamp: "max" }
        else:
            msg = 'Invalid time aggregate %s. Use "first" or "last"' %time_agg
            raise ValueError(msg)
        
        #build query b aggregated
        (query_b,table,dim,pandas_aggregate,agg_dict) = self.query_agg(
                    agg_dict = time_agg_dict,
                    table_name = table_name,
                    schema = schema,
                    groupby = groupby,
                    time_grain = time_grain,
                    timestamp = timestamp,
                    dimension = None,
                    start_ts = start_ts,
                    end_ts = end_ts,
                    entities = entities
                )
        
        right_timestamp = '%s_%s' %(time_agg,timestamp)
        keys = [(timestamp,right_timestamp)]
        keys.extend(groupby)
        right_cols = { right_timestamp: right_timestamp,
                       timestamp : timestamp }
        query = self.subquery_join(query_a,query_b,*keys,**right_cols)
        
        return(query,table)
    
    
    def unregister_functions(self,function_names):
        '''
        Unregister functions by name. Accepts a list of function names.
        '''
        if not isinstance(function_names,list):
            function_names = [function_names]
    
        for f in function_names:
            payload = {
                'name' : f
                }
            r = self.http_request(object_type='function',object_name=f, request = 'DELETE', payload=payload)
            try:
                msg = 'Function registration deletion status: %s' %(r.data.decode('utf-8'))
            except AttributeError:
                msg = 'Function registration deletion status: %s' %r
            logger.info(msg) 
            
    def unregister_constants(self,constant_names):
        '''
        Unregister constants by name.
        '''

        if not isinstance(constant_names,list):
            constant_names = [constant_names]
        payload = []
    
        for f in constant_names:
            payload.append( {
                'name' : f,
                'entityType' : None
                }
            )
            
        r = self.http_request(object_type='defaultConstants',object_name=f, request = 'DELETE', payload=payload)
        try:
            msg = 'Constants deletion status: %s' %(r.data.decode('utf-8'))
        except AttributeError:
            msg = 'Constants deletion status: %s' %r
        logger.info(msg) 
        
    
    def write_frame(self,df,
                    table_name, 
                    version_db_writes = False,
                    if_exists = 'append',
                    timestamp_col = None,
                    schema = None,
                    chunksize = None):
        '''
        Write a dataframe to a database table
        
        Parameters
        ---------------------
        db_credentials: dict (optional)
            db2 database credentials. If not provided, will look for environment variable
        table_name: str 
            table name to write to.
        version_db_writes : boolean (optional)
            Add seprate version_date column to table. If not provided, will use default for instance / class
        if_exists : str (optional)
            What to do if table already exists. If not provided, will use default for instance / class
        chunksize : int
            batch size for writes
        Returns
        -----------
        numerical status. 1 for successful write.
            
        '''
        
        if chunksize is None:
            chunksize = self.write_chunk_size
            
        df = df.reset_index()
        # the column names id, timestamp and index are reserverd as level names. They are also reserved words
        # in db2 so we don't use them in db2 tables.
        # deviceid and evt_timestamp are used instead
        if 'deviceid' not in df.columns and 'id' in df.columns:
            df['deviceid'] = df['id']
            df = df[[x for x in df.columns if x !='id']]
        if timestamp_col is not None and timestamp_col not in df.columns and '_timestamp' in df.columns:
            df[timestamp_col] = df['_timestamp']
            df = df[[x for x in df.columns if x !='_timestamp']]
        df = df[[x for x in df.columns if x !='index']]
        if version_db_writes:
            df['version_date'] = dt.datetime.utcnow()
        if table_name is None:
            raise ValueError('Function attempted to write data to a table. A name was not supplied. Specify an instance variable for out_table_name. Optionally include an out_table_prefix too')
        dtypes = {}        
        #replace default mappings to clobs and booleans
        for c in list(df.columns):
            if is_string_dtype(df[c]):
                dtypes[c] = String(255)
            elif is_bool_dtype(df[c]):
                dtypes[c] = SmallInteger()
        table_exists = False
        cols = None
        if if_exists == 'append':
            #check table exists
            try:
                table = self.get_table(table_name,schema)
            except KeyError:
                pass
            else:
                table_exists = True
                cols = [column.key for column in table.columns]
                extra_cols = set([x for x in df.columns if x !='index'])-set(cols)
                if len(extra_cols) > 0:
                    logger.warning('Dataframe includes column/s %s that are not present in the table. They will be ignored.' %extra_cols)            
                try: 
                    df = df[cols]
                except KeyError:
                    raise KeyError('Dataframe does not have required columns %s' %cols)                
        self.start_session()
        try:        
            df.to_sql(name = table_name, con = self.connection, schema = schema,
                  if_exists = if_exists, index = False, chunksize = chunksize, dtype = dtypes)
        except:
            self.session.rollback()
            logger.info('Attempted write of %s data to table %s ' %(cols,table_name))
            raise
        finally:
            self.commit()
            logger.info('Wrote data to table %s ' %table_name)
        return 1    
        
class BaseTable(object):

    is_table = True
    _entity_id = 'deviceid'
    _timestamp = 'evt_timestamp'
    
    def __init__ (self,name,database,*args, **kw):
        as_keywords = ['_timestamp','_timestamp_col','_activities','_freq','_entity_id','_df_index_entity_id','_tenant_id']
        self.name = name
        self.database= database
        # the keyword arguments may contain properties and sql alchemy dialect specific options
        # set them in child classes before calling super._init__()
        # self.set_params(**kw)
        # delete the designated AS metadata properties as sql alchemy will not understand them
        for k in as_keywords:
            try:
                del kw[k]
            except KeyError:
                pass
        kw['extend_existing'] = True
        try: 
            kwschema = kw['schema']
        except KeyError:
            try:
                kw['schema'] = kw['_db_schema']
            except KeyError:
                msg = 'No schema specified as **kw, using default for table %s' %self.name
                logger.warning(msg)
        else:
            if kwschema is None:
                msg = 'Schema passed as None, using default schema'
                logger.debug(msg)            
        self.table = Table(self.name,self.database.metadata, *args,**kw )
        self.id_col = Column(self._entity_id,String(50))
        
    def create(self):
        self.table.create()
        
    def get_column_names(self):
        """
        Get a list of columns names
        """
        return [column.key for column in self.table.columns]                

    def insert(self,df, chunksize = None):
        """
        Insert a dataframe into table. Dataframe column names are expected to match table column names.
        """
        
        if chunksize is None:
            chunksize = self.database.write_chunk_size
        
        df = df.reset_index()
        cols = self.get_column_names()
        
        extra_cols = set([x for x in df.columns if x !='index'])-set(cols)            
        if len(extra_cols) > 0:
            logger.warning('Dataframe includes column/s %s that are not present in the table. They will be ignored.' %extra_cols)
            
        dtypes = {}        
        #replace default mappings to clobs and booleans
        for c in list(df.columns):
            if is_string_dtype(df[c]):
                dtypes[c] = String(255)
            elif is_bool_dtype(df[c]):
                dtypes[c] = SmallInteger()
                
        try: 
            df = df[cols]
        except KeyError:
            msg = 'Dataframe does not have required columns %s. It has columns: %s and index: %s' %(cols,df.columns,df.index.names)
            raise KeyError(msg)
        self.database.start_session()
        try:        
            df.to_sql(name = self.name, con = self.database.connection, schema = self.schema,
                  if_exists = 'append', index = False, chunksize = chunksize,dtype=dtypes)
        except:
            self.database.session.rollback()
            raise
        finally:
            self.database.session.close()
        
            
    def set_params(self, **params):
        '''
        Set parameters based using supplied dictionary
        '''
        for key,value in list(params.items()):
            setattr(self, key, value)
        return self               
            
    def query(self):
        """
        Return a sql alchemy query object for the table. 
        """        
        (q,table) =self.database.query(self.table)
        return (q,table)           


class SystemLogTable(BaseTable):
    """
    A log table only has a timestamp as a predefined column
    """
    
    def __init__(self,name,database,*args,**kw):
 
        self.timestamp = Column(self._timestamp,DateTime)
        super().__init__(name,database,self.timestamp,*args, **kw)
        

class ActivityTable(BaseTable):
    """
    An activity table is a special class of table that iotfunctions understands to contain data containing activities performed using or on an entity.
    The table contains a device id, start date and end date of the activity and an activity code to indicate what type of activity was performed.
    The table can have any number of additional Column objects supplied as arguments.
    Also supply a keyword argument containing "activities" a list of activity codes contained in this table
    """
        
    def __init__ (self,name,database,*args, **kw):
        self.set_params(**kw)
        self.id_col = Column(self._entity_id,String(50))
        self.start_date = Column('start_date',DateTime)
        self.end_date = Column('end_date',DateTime)
        self.activity = Column('activity',String(255))
        super().__init__(name,database,self.id_col,self.start_date,self.end_date,self.activity, *args, **kw)
        

class Dimension(BaseTable):
    """
    A dimension contains non time variant entity attributes. 
    """
    def __init__ (self,name,database,*args, **kw):
        self.set_params(**kw)
        self.id_col = Column(self._entity_id,String(50))
        super().__init__(name,database,self.id_col,
                 *args, **kw)
    
        
class ResourceCalendarTable(BaseTable):
    """
    A resource calendar table is a special class of table that iotfunctions understands to contain data that can be used to understand what resource/s were assigned to an entity
    The table contains a device id, start date and end date and the resource_id. 
    Create a separte table for each different type of resource, e.g. operator, owner , company
    The table can have any number of additional Column objects supplied as arguments.
    """            
    def __init__ (self,name,database,*args, **kw):
        self.set_params(**kw)
        self.start_date = Column('start_date',DateTime)
        self.end_date = Column('end_date',DateTime)
        self.resource_id = Column('resource_id',String(255))
        self.id_col = Column(self._entity_id,String(50))
        super().__init__(name,database,self.id_col,self.start_date,self.end_date,self.resource_id, *args, **kw)
        
class TimeSeriesTable(BaseTable):
    """
    A time series table contains a timestamp and one or more metrics.
    """
    def __init__ (self,name,database,*args, **kw):
        
        self.set_params(**kw)
        self.id_col = Column(self._entity_id,String(256))
        self.evt_timestamp = Column(self._timestamp,DateTime)
        self.device_type = Column('devicetype',String(64))
        self.logical_interface = Column('logicalinterface_id',String(64))
        self.event_type = Column('eventtype',String(64))
        self.format = Column('format',String(32))
        self.updated_timestamp = Column('updated_utc',DateTime)
        super().__init__(name,database,self.id_col,self.evt_timestamp,
                 self.device_type, self.logical_interface, self.event_type,
                 self.format, 
                 self.updated_timestamp,
                 *args, **kw)
        
class SlowlyChangingDimension(BaseTable):
    """
    A slowly changing dimension table tracks changes to a property of an entitity over time
    The table contains a device id, start date and end date and the property 
    Create a separate table for each property, e.g. firmware_version, owner
    """    
        
    def __init__ (self,name,database,property_name,datatype,*args,**kw):
        self.set_params(**kw)
        self.start_date = Column('start_date',DateTime)
        self.end_date = Column('end_date',DateTime)
        self.property_name = Column(property_name,datatype)
        self.id_col = Column(self._entity_id,String(50))
        super().__init__(name,database,self.id_col,self.start_date,self.end_date,self.property_name,**kw )

