import inspect
import logging
import datetime as dt
import math
from sqlalchemy.sql.sqltypes import TIMESTAMP,VARCHAR
import numpy as np
import pandas as pd

from iotfunctions.base import BaseTransformer
from iotfunctions import ui

logger = logging.getLogger(__name__)


# Specify the URL to your package here.
# This URL must be accessible via pip install

PACKAGE_URL = 'git+https://github.com/rweidinger/functions@'

class PrevDayHourlyAvgDiffTest1(BaseTransformer):
   '''
   Calculate the mean of a data item from yesterday during the same hour as current hour
   '''

   execute_by = ['deviceid']

   def __init__(self, input_item, output_item='output_item'):
       self.input_item = input_item
       self.output_item = output_item
       super().__init__()

   def _calc(self, df):
       df = df.copy()
       today = dt.datetime.today()
       yesterday = today - dt.timedelta(days=1)
       yesterday_date = yesterday.date()
       yesterday_hour = yesterday.hour
       yesterday_values_hour = df.loc[(df['_timestamp'].dt.date == yesterday_date) &
                                      (df['_timestamp'].dt.hour == yesterday_hour)][self.input_item]
       df[self.output_item] = df[self.input_item] - yesterday_values_hour.mean()
       return df

   @classmethod
   def build_ui(cls):
       # define arguments that behave as function inputs
       inputs = []
       inputs.append(ui.UISingleItem(
           name='input_item',
           datatype=float,
           description='Data item to calculate average of same hour of previous day'
       ))
       # define arguments that behave as function outputs
       outputs = []
       outputs.append(ui.UIFunctionOutSingle(
           name='PrevDayHourlyAvgDiff',
           datatype=float,
           description='PrevDayHourlyAvgDiff'
       ))
       return inputs, outputs

class PrevDayHourlyAvgPercentDiffTest1(BaseTransformer):
   '''
   Calculate the mean of a data item from yesterday during the same hour as current hour
   '''

   execute_by = ['deviceid']

   def __init__(self, input_item, output_item='output_item'):
       self.input_item = input_item
       self.output_item = output_item
       super().__init__()

   def _calc(self, df):
       df = df.copy()
       today = dt.datetime.today()
       yesterday = today - dt.timedelta(days=1)
       yesterday_date = yesterday.date()
       yesterday_hour = yesterday.hour
       yesterday_values_hour = df.loc[(df['_timestamp'].dt.date == yesterday_date) &
                                      (df['_timestamp'].dt.hour == yesterday_hour)][self.input_item]
       yesterday_values_hour_mean = yesterday_values_hour.mean()
       df[self.output_item] = ((df[self.input_item] - yesterday_values_hour_mean) / abs(yesterday_values_hour_mean)) * 100
       return df

   @classmethod
   def build_ui(cls):
       # define arguments that behave as function inputs
       inputs = []
       inputs.append(ui.UISingleItem(
           name='input_item',
           datatype=float,
           description='Data item to calculate average of same hour of previous day'
       ))
       # define arguments that behave as function outputs
       outputs = []
       outputs.append(ui.UIFunctionOutSingle(
           name='PrevDayHourlyAvgDiff',
           datatype=float,
           description='PrevDayHourlyAvgDiff'
       ))
       return inputs, outputs