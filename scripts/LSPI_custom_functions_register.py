import json
from custom.db import Database
from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, func


with open('credentials.json', encoding='utf-8') as F:
    credentials = json.loads(F.read())

db_schema = None

db = Database(credentials=credentials)


from custom.functions import PrevDayHourlyAvgDiffTest1 # , PrevDayHourlyAvgPercentDiffTest
db.register_functions([PrevDayHourlyAvgDiffTest1])
















