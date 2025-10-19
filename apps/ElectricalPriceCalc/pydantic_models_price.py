from datetime import datetime, timedelta
from pydantic import BaseModel


class PeakHour(BaseModel):
    start: datetime
    end: datetime
    duration: timedelta

class PriceHour(BaseModel):
    start: datetime
    end: datetime
    value: float