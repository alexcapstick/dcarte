from pathlib import Path
import json
from functools import wraps
import collections
import typing

from copy import deepcopy
import time
import datetime as dt
from scipy.stats import circmean,circstd
from scipy.signal import savgol_filter
import pyarrow.parquet as pq
import dateutil
import os
import pyarrow as pa
import requests
import yaml
import zipfile
import pandas as pd
import numpy as np


def segment_freq(v:pd.Series,
                 window_length:int=59,
                 polyorder:int=1,
                 r:int=10,
                 lb:float=0.0,
                 ub:float=360.0,
                 step:float=0.25)-> pd.DataFrame:
    """segment_freq [summary]

    [extended_summary]

    Args:
        v (pd.Series): [description]
        window_length (int, optional): [description]. Defaults to 59.
        polyorder (int, optional): [description]. Defaults to 1.
        r (int, optional): [description]. Defaults to 10.

    Returns:
        pd.DataFrame: [description]
    """
    ix = np.arange(lb,ub,step)
    vc = v.value_counts(normalize=False).sort_index().rename('freq').to_frame()
    vc = vc.reindex(ix)
    vc = vc.assign(freq_smooth = savgol_filter(vc.freq, window_length, polyorder))
    x = vc.freq_smooth.values
    vc = vc.assign(freq_cumsum=vc.freq_smooth.cumsum())
    L,idx,m = x**0-1,np.argsort(-x),1
    d,n = np.mgrid[-r:r],idx.shape[0]-1
    for i in idx:
        ix = np.clip(i+d,0,n)
        local = L[ix][L[ix]>0]
        labels, counts = np.unique(local, return_counts=True)
        if local.size==0: L[i],m=m,m+1
        if labels.size==1:L[i]=labels[0]
        if labels.size>1:L[i]=labels[np.argmax(counts)]
    vc = vc.assign(labels=L)
    return vc



def dst_correction(df, dst_correct=True, years=[2018, 2019, 2020, 2021,2022], shift_time=12):
    ''' day light saving correction 
    Goes over timestamps and identifies dst windows and shifts them back 1 hour
    this is to correct utc timestamps applied at midday before the corrections
    to avoid removing sleep data
    Dr Eyal Soreq 06.04.21 UKDRI
    '''
    if dst_correct:
        
        df = df.set_index('start_date').sort_index()
        dst = pd.DataFrame({'year': years,
                            "dst_start": [last_sunday_date(y, month=3) for y in years],
                            "dst_end": [last_sunday_date(y, month=10) for y in years]})
        dates = dst[["dst_start", "dst_end"]].values.reshape(-1)
        _df = []
        for ii, (st, et) in enumerate(zip(dates[:-1], dates[1:])):
            shift = -1 if (st.month <= 11 and st.month >= 4) else 0
            st = str((st-timedelta(hours=shift_time))[0])
            et = str((et-timedelta(hours=shift_time))[0])
            _df.append(df[st:et].shift(shift, 'H'))
        df = pd.concat(_df)
        # localize_time(timezones, df, factors)
    return df


def last_sunday_date(year=2019, month=10):
    last_sunday = max(week[-1] for week in calendar.monthcalendar(year, month))
    return pd.DatetimeIndex([f'{year}-{month}-{last_sunday}'])


def segment_summary(vc,shift=180):
    """segment_summary [summary]

    [extended_summary]

    Args:
        vc ([type]): [description]
    """
    def f(x): return dt.time(*angles_to_time(x))
    summary = (vc.reset_index().
                    groupby('labels').
                    agg(start=('index','first'),
                        end=('index','last'),
                        proportion=('freq','sum')))
    summary.start = ((summary.start-shift)%360).transform(f)
    summary.end = ((summary.end-shift)%360).transform(f)

    return summary

def round_to_quarters(number):
    """Round a number to the closest half integer.
    >>> round_to_quarters(1.3)
    1.25
    >>> round_to_quarters(2.6)
    2.5
    >>> round_to_quarters(3.0)
    3.0
    >>> round_to_quarters(4.1)
    4.0"""

    return np.round(number * 4) / 4

def center_angle(x): 
    return (np.deg2rad(x) + np.pi) % (2*np.pi) - np.pi


def round_to_halves(number):
    """Round a number to the closest half integer.
    >>> round_to_halves(1.3)
    1.5
    >>> round_to_halves(2.6)
    2.5
    >>> round_to_halves(3.0)
    3.0
    >>> round_to_halves(4.1)
    4.0"""

    return np.round(number * 2) / 2


def timer(desc : str = None):
    """timer is a wrapper decorator to report functions duration
    Args:
        desc (str, optional): [description line to print to sdout]. Defaults to None.
    """
    def wrapper(fun):
        @wraps(fun)
        def wrapped(*fun_args, **fun_kwargs):
            start = time.perf_counter()
            if len(fun_args)>0 and isinstance(fun_args[0], str):
                prefix = f'Finished {desc} {fun_args[0]} in:'
            else:
                prefix = f'Finished {desc} in:'
            out = fun(*fun_args, **fun_kwargs)
            elapsed = time.perf_counter() - start
            dur = f'{np.round(elapsed,1)}'
            print(f"{prefix:<40}{dur:>10} {'seconds':<10}")
            return out
        return wrapped
    return wrapper


class BearerAuth(requests.auth.AuthBase):
    """BearerAuth manages the coupling of a token to requests framework

    Args:
        requests ([type]): [description]
    """
    def __init__(self, token):
        self.token = token

    def __call__(self, r):
        r.headers["authorization"] = "Bearer " + self.token
        return r


def isnotebook() -> bool:
    """isnotebook checks if the run environment is a jupyter notebook


    Returns:
        bool: [description]
    """
    try:
        shell = True#get_ipython().__class__.__name__
        if shell == 'ZMQInteractiveShell':
            return True
        elif shell == 'TerminalInteractiveShell':
            return False
        else:
            return False
    except NameError:
        return False


def date2iso(date: str, output_fmt: str ='%Y-%m-%dT%H:%M:%S.%f'):
    """date2iso convert a date string to iso format 

    Args:
        date (str): [description]
        output_fmt (str, optional): [description]. Defaults to '%Y-%m-%dT%H:%M:%S.%f'.

    Returns:
        [type]: [description]
    """
    dt = dateutil.parser.parse(date)
    return dt.strftime(output_fmt)+'Z'


def read_table(filename: str, columns: list = None) -> pd.DataFrame:
    """read_table reads a parquet pyspark file 

    Args:
        filename (str): filename either in relative or in absoulte path 
        columns (list, optional): specific columns to load. Defaults to None.

    Returns:
        pd.DataFrame: [description]
    """
    if columns is None:
        data = pq.read_table(filename).to_pandas()
    else:
        data = pq.read_table(filename, columns=columns).to_pandas()
    return data


def update_table(data: pd.DataFrame,
                 filename: str,
                 compression: str,
                 meta_content: dict = {}) -> None:
    """update_table updates a parquet pyspark file 

    Args:
        data (pd.DataFrame): [description]
        filename (str): [description]
        compression (str): [description]
        meta_content (dict, optional): [description]. Defaults to {}.
    """
    table = pa.Table.from_pandas(data)
    table = inject_metadata(table, meta_content)
    pqwriter = pq.ParquetWriter(
        filename, table.schema, compression=compression)
    pqwriter.write_table(table=table)
    pqwriter.close()


def inject_metadata(table: pa.table, meta_content: dict) -> pa.Table:
    """inject_metadata replaces metadata in a parquet pyspark file 

    [extended_summary]

    Args:
        table (pa.table): [description]
        meta_content (dict): [description]

    Returns:
        pa.Table: [description]
    """
    pandas_meta = table.schema.metadata
    metadata = json.dumps(meta_content)
    metadata = {
        'minder'.encode(): metadata.encode(),
        **pandas_meta
    }
    table = table.replace_schema_metadata(metadata)
    return table


def write_table(data: pd.DataFrame,
                filename: str,
                compression: str,
                meta_content: dict = {}) -> None:
    """write_table writes data into a parquet pyspark file 

    [extended_summary]

    Args:
        data (pd.DataFrame): [description]
        filename (str): [description]
        compression (str): [description]
        meta_content (dict, optional): [description]. Defaults to {}.
    """
    if filename.split('.')[-1] != 'parquet':
        filename = f'{filename}.parquet'
    data = data[data.columns.drop_duplicates()]    
    table = pa.Table.from_pandas(data)
    table = inject_metadata(table, meta_content)
    pq.write_table(table, filename, compression=compression)


def read_metadata(filename: str):
    """read_metadata return only the metadata from a parquet pyspark file 

    Args:
        filename (str): [description]

    Returns:
        [type]: [description]
    """
    return pq.read_metadata(filename)


def write_yaml(local_file:str, data:dict):
    """write_yaml writes a dictionary structure into a yaml file

    Args:
        local_file (str): [description]
        data (dict): [description]
    """

    with open(local_file, 'w') as yamlfile:
        yaml.safe_dump(data, yamlfile)


def update_yaml(local_file: str, data: dict):
    """update_yaml updates a dictionary structure onto a yaml file

    [extended_summary]

    Args:
        local_file (str): [description]
        data (dict): [description]
    """
    with open(local_file, 'r') as yamlfile:
        currdata = yaml.safe_load(yamlfile)
        data_merged = merge_dicts(currdata,data)
    if  currdata != data_merged:
        write_yaml(local_file, data_merged)


def load_csv_from_zip(_zip, csv_file):
        df = pd.read_csv(_zip.open(csv_file),
                         encoding='unicode_escape', low_memory=False)
        return df

def load_yaml(local_file: str) -> dict :
    """load_yaml loads a yaml file into a dictionary

    Args:
        local_file (str): [description]

    Returns:
        dict: [description]
    """
    with open(local_file, 'r') as yamlfile:        
        return yaml.safe_load(yamlfile)


def path_exists(local_file:str):
    """path_exists checks if a file exists in the local filesystem

    [extended_summary]

    Args:
        local_file (str): [description]

    Returns:
        [type]: [description]
    """
    return Path(local_file).exists()


def set_path(local_file: str):
    """set_path checks if a parent folder exists and if not creates it 
    Args:
        local_file (str): [description]
    """
    if not path_exists(local_file):
        Path(Path(local_file).absolute()
             .parent).mkdir(parents=True, exist_ok=True)

def merge_dicts(d1:dict, d2:dict):
    """merge_dicts merges two dictionaries

    Args:
        d1 (dict): [description]
        d2 (dict): [description]

    Yields:
        [type]: [description]
    """
 


    
    result = deepcopy(d1)
    for k2, v2 in d2.items():
        v1 = result.get(k2)
        if isinstance(v1, dict) and isinstance(v2, dict):
            result[k2] = merge_dicts(v1, v2)
        else:
            result[k2] = deepcopy(v2)
    return result
            


def lagged_df(df:pd.DataFrame, factor:str='activity', lags:int=7):
    """lagged_df returns a lagged dataframe for a specific factor 

    [extended_summary]

    Args:
        df (pd.DataFrame): [description]
        factor (str, optional): [description]. Defaults to 'activity'.
        lags (int, optional): [description]. Defaults to 7.

    Returns:
        [type]: [description]
    """
    return pd.concat([df[factor].shift(lag).rename(f't(-{lags-lag-1})') for lag in range(lags)], axis=1).dropna()


def reindex_ts(x:pd.Series, freq:str):
    """reindex_ts reindex a timeseries by some freq 

    Args:
        x (pd.Series): [description]
        freq (str): goes in the time format of pandas 

    Returns:
        [type]: [description]
    """
    rng = pd.date_range(x.index.min(), x.index.max(), freq=freq)
    _df = pd.DataFrame(x, index=x.index).reindex(rng)
    return _df


def load_zip_csv(zip_file:str, csv_file:str) -> pd.DataFrame:
    """load_zip_csv returns a specific csv file from a zip file 

    Args:
        zip_file (str): [description]
        csv_file (str): [description]

    Returns:
        pd.DataFrame: [description]
    """
    zip = zipfile.ZipFile(zip_file)
    df = pd.read_csv(zip.open(csv_file),
                     encoding='unicode_escape',
                     low_memory=False)
    return df


def shift_row_to_bottom(df:pd.DataFrame, index_to_shift:str)-> pd.DataFrame:
    """shift_row_to_bottom shifts a specific index to the bottom of a dataframe
    Args:
        df (pd.DataFrame): [description]
        index_to_shift (str): [description]

    Returns:
        pd.DataFrame: [description]
    """
    idx = [i for i in df.index if i != index_to_shift]
    return df.loc[idx+[index_to_shift]]


def shift_row_to_top(df: pd.DataFrame, index_to_shift: str) -> pd.DataFrame:
    """shift_row_to_top shifts a specific index to the top of a dataframe

    Args:
        df (pd.DataFrame): [description]
        index_to_shift (str): [description]

    Returns:
        pd.DataFrame: [description]
    """
    idx = [i for i in df.index if i != index_to_shift]
    return df.loc[[index_to_shift]+idx]


def localize_time(df:pd.DataFrame, factors:list, timezones=None):
    """localize_time [summary]

    [extended_summary]

    Args:
        df (pd.DataFrame): [description]
        factors (list): [description]
        timezones ([type], optional): [description]. Defaults to None.

    Returns:
        [type]: [description]
    """
    data = []
    if timezones is None:
        timezones = ['Europe/London']
        df['timezone'] = np.repeat('Europe/London', df.shape[0])
    for tz in timezones:
        _df = df[df.timezone == tz].copy()
        try:
            for factor in factors:
                dt = pd.to_datetime(_df[factor],utc=True).dt.tz_localize(None)
                offset = pd.Series([t.utcoffset() for t in dt.dt.tz_localize(
                    tz, ambiguous=True, nonexistent='shift_forward')], index=dt.index)
                _df[factor] = dt + offset
            data.append(_df)
        except:
            print(tz)
    data = pd.concat(data)
    return data

def rolling_window(a, window:int):
    shape = a.shape[:-1] + (a.shape[-1] - window + 1, window)
    strides = a.strides + (a.strides[-1],)
    c = np.lib.stride_tricks.as_strided(a, shape=shape, strides=strides)
    seq = ['>'.join(s) for s in c]
    return seq


def mine_pathway(df:pd.DataFrame,
                 value:str = 'location_name',
                 source:str='bed_out',
                 sink:str='bed_in',
                 min_dur:float=180,
                 max_dur:float=900):
    transitions = mine_transition(df.query(f'{value} in ["{source}","{sink}"]'),
                                  value=value)
    if not transitions.empty:
        events = (transitions.
                query(f'transition == "{source}>{sink}"').
                query(f'dur>{min_dur} and dur<{max_dur}'))
        pattern = []
        for s,e in zip(events.start_date,events.end_date):
            tmp = ">".join(df.set_index('start_date').loc[s:e].location_name.values)
            pattern.append(tmp)
        events['pathway'] = pattern
        return events   
    else:
        return pd.DataFrame()
    
def mine_transition(df,value:str,datetime:str='start_date',window:int=1):
    # TODO: add categorical capabilites
    df = df.sort_values(datetime).drop_duplicates().reset_index()
    if not df.empty:
       dur = (df[datetime].shift(-window) - 
              df[datetime]).dt.total_seconds().rename('dur')
       start_date = df[datetime].rename('start_date')
       end_date = df[datetime].shift(-window).rename('end_date')
       source = df[value].rename('source')
       sink = df[value].shift(-window).rename('sink')
       transition = pd.Series(rolling_window(df[value].values,window+1)).rename('transition')
       return pd.concat([start_date, end_date, source,sink,transition.reindex(sink.index), dur], axis=1)
    else:
       return pd.DataFrame()    

def between_time(df,factor,start_time,end_time):
    index = pd.DatetimeIndex(df[factor])
    return df.iloc[index.indexer_between_time(start_time,end_time)]


def epoch_to_local(dt: pd.Series, tz: str = 'Europe/London', unit: str = 's', shift: int = 0)-> pd.Series:
    """epoch_to_local converts epoch

    [extended_summary]

    Args:
        dt (pd.Series): [description]
        tz (str, optional): [description]. Defaults to 'Europe/London'.
        unit (str, optional): [description]. Defaults to 's'.
        shift (int, optional): [description]. Defaults to 0.

    Returns:
        pd.Series: [description]
    """
    dt = pd.to_datetime(dt, unit=unit, utc=True)
    offset = pd.Series([t.utcoffset() for t in dt.dt.tz_localize(
        tz, ambiguous='infer', nonexistent='shift_backward')], index=dt.index)
    return dt + offset + pd.Timedelta(hours=shift)

def utc_to_local(dt:pd.Series, tz:str='Europe/London', shift:int=-2):
    """utc_to_local converts a timeseries from utc to a specific timezone 

    Args:
        dt (pd.Series): [description]
        tz (str, optional): [description]. Defaults to 'Europe/London'.
        shift (int, optional): [description]. Defaults to -2.

    Returns:
        [type]: [description]
    """
    dt = pd.to_datetime(dt,utc=True).dt.tz_localize(None)
    offset = pd.Series([t.utcoffset() for t in dt.dt.tz_localize(
        tz, ambiguous='infer', nonexistent='shift_backward')], index=dt.index)
    return dt + offset + pd.Timedelta(hours=shift)


def time_cdf(times:pd.Series, name:str)->pd.Series:
    """time_cdf return a cdf of times as a pandas Series

    Args:
        times (pd.Series): [description]
        name (str): [description]

    Returns:
        pd.Series: [description]
    """
    vc = times.value_counts(normalize=True).sort_index()
    rng = pd.date_range('2021-01-01 12:00:00',
                        '2021-01-02 12:00:00', freq='1T')
    vc = vc.reindex(rng.time)
    vc = vc.fillna(0).cumsum()
    vc.name = name
    return vc



def process_transition(df:pd.DataFrame, groupby:list, datetime:str, value:str, covariates=None) -> pd.DataFrame:
    """process_transition convert a timeseries DataFrame with datetimes to a transition dataframe 

    Args:
        df (pd.DataFrame): [description]
        groupby (list): [description]
        datetime (str): [description]
        value (str): [description]
        covariates ([type], optional): [description]. Defaults to None.

    Returns:
        [type]: [description]
    """
    data = []
    df = df.sort_values(datetime)
    for ix, subset in df.groupby(groupby):
        if len(groupby) > 1:
            index = pd.MultiIndex.from_tuples(
                [ix for _ in range(subset.shape[0])], names=groupby)
        else:
            index = np.tile(ix, subset.shape[0])
        subset = subset.reset_index(drop=True)
        dur = (subset[datetime].shift(-1) - subset[datetime]
               ).dt.total_seconds().rename('dur')
        start_date = subset[datetime].rename('start_date')
        end_date = subset[datetime].shift(-1).rename('end_date')
        source = subset[value].astype(str).rename('source')
        sink = subset[value].shift(-1).astype(str).rename('sink')
        transition = (source + '>' + sink).rename('transition')
        if covariates is None:
            subset = pd.concat([start_date, end_date, source,sink,transition, dur], axis=1)
        else:
            cov = subset[covariates]
            subset = pd.concat([start_date, end_date , source , sink, cov, transition, dur], axis=1)
            
        subset.index = index
        subset.index.names = groupby
        data.append(subset.dropna())
    data = pd.concat(data)
    dtypes = {'start_date': 'datetime64',
              'end_date': 'datetime64',
              'source':'category',
              'sink':'category',
              'transition': 'category',
              'dur': 'float'}
    return data.astype(dtypes)


def time_to_angles(time, day=24*60**2):
    """time_to_angles [summary]

    [extended_summary]

    Args:
        time ([type]): [description]
        day ([type], optional): [description]. Defaults to 24*60**2.

    Returns:
        [type]: [description]
    """
    time =  str_to_time(time) if str is type(time) else time
    return 360*(time.hour*60**2+time.minute*60+time.second)/day 


def str_to_time(time):
    """str_to_time [summary]

    [extended_summary]

    Args:
        time ([type]): [description]

    Returns:
        [type]: [description]
    """
    return dt.time(*[int(t) for t in time.split(':')])

def seconds_to_time(seconds):
    """seconds_to_time [summary]

    [extended_summary]

    Args:
        seconds ([type]): [description]

    Returns:
        [type]: [description]
    """
    if pd.isnull(seconds):
        return np.full(3,np.nan)
    else:
        h, m = np.divmod(seconds, 3600)
        m, s = np.divmod(m, 60)
        hms = np.array([h, m, s]).astype(int)
        return hms.T

def angles_to_time(angles, day=24*60**2):
    """angles_to_time [summary]

    [extended_summary]

    Args:
        angles ([type]): [description]
        day ([type], optional): [description]. Defaults to 24*60**2.

    Returns:
        [type]: [description]
    """
    return seconds_to_time((angles * day) / 360)

def times_to_angles(times, day=24*60**2):
    """times_to_angles [summary]

    [extended_summary]

    Args:
        times ([type]): [description]
        day ([type], optional): [description]. Defaults to 24*60**2.

    Returns:
        [type]: [description]
    """
    return np.array([time_to_angles(t,day) for t in times])    

def std_time(times,kind='time'):
    """std_time [summary]

    [extended_summary]

    Args:
        times ([type]): [description]
        kind (str, optional): [description]. Defaults to 'time'.

    Returns:
        [type]: [description]
    """
    day = 24*60**2
    angles = times_to_angles(times, day)
    sd_angle = circstd(angles,high=360)
    if kind == 'time':
        return dt.time(*angles_to_time(sd_angle))#np.timedelta64(sd_seconds,'s')
    if kind == 'angles':
        return sd_angle
    if kind == 'datetime':
        return dt.datetime(2021,1,1,*angles_to_time(sd_angle))
    if kind == 'timedelta':
        return dt.timedelta(seconds=(sd_angle * day) / 360)
   
def mean_time(times,kind='time'):
    """mean_time [summary]

    [extended_summary]

    Args:
        times ([type]): [description]
        kind (str, optional): [description]. Defaults to 'time'.

    Returns:
        [type]: [description]
    """
    day = 24*60**2
    angles = times_to_angles(times, day)
    mean_angle = circmean(angles,high=360)
    if kind == 'time':
        return dt.time(*angles_to_time(mean_angle))#np.timedelta64(sd_seconds,'s')
    if kind == 'angles':
        return mean_angle
    if kind == 'datetime':
        return dt.datetime(2021,1,1,*angles_to_time(mean_angle))
    if kind == 'timedelta':
        return dt.timedelta(seconds=(mean_angle * day) / 360)



def shift_row_to_bottom(df, index_to_shift):
    """shift_row_to_bottom [summary]

    [extended_summary]

    Args:
        df ([type]): [description]
        index_to_shift ([type]): [description]

    Returns:
        [type]: [description]
    """
    idx = [i for i in df.index if i!=index_to_shift]
    return df.loc[idx+[index_to_shift]]

def shift_row_to_top(df, index_to_shift):
    """shift_row_to_top [summary]

    [extended_summary]

    Args:
        df ([type]): [description]
        index_to_shift ([type]): [description]

    Returns:
        [type]: [description]
    """
    idx = [i for i in df.index if i!=index_to_shift]
    return df.loc[[index_to_shift]+idx]