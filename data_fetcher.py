import argparse
import pandas as pd
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path
import pytz


class DataSource(ABC):
    @abstractmethod
    def fetch(self, symbol: str, days: int, interval: str) -> pd.DataFrame:
        pass


class InteractiveBrokersSource(DataSource):
    def __init__(self, host: str = '127.0.0.1', port: int = 7497, client_id: int = 1):
        self.host = host
        self.port = port
        self.client_id = client_id

    def fetch(self, symbol: str, days: int, interval: str) -> pd.DataFrame:
        try:
            from ib_insync import IB, Future, util
        except ImportError:
            raise ImportError("ib_insync not installed. Run: pip install ib_insync")

        ib = IB()
        try:
            ib.connect(self.host, self.port, clientId=self.client_id)

            contract = Future(symbol, exchange='COMEX')
            ib.qualifyContracts(contract)

            bar_size = self._convert_interval(interval)
            end_dt = datetime.now()
            duration = f'{days} D'

            bars = ib.reqHistoricalData(
                contract,
                endDateTime=end_dt,
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow='TRADES',
                useRTH=False,
                formatDate=1
            )

            df = util.df(bars)
            if df is not None and not df.empty:
                df = df.rename(columns={
                    'date': 'timestamp',
                    'open': 'open',
                    'high': 'high',
                    'low': 'low',
                    'close': 'close',
                    'volume': 'volume'
                })
                df = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]
                df['timestamp'] = pd.to_datetime(df['timestamp'])
                return df

            return pd.DataFrame()

        finally:
            ib.disconnect()

    def _convert_interval(self, interval: str) -> str:
        mapping = {
            '1min': '1 min',
            '3min': '3 mins',
            '5min': '5 mins',
            '15min': '15 mins',
            '1hour': '1 hour'
        }
        return mapping.get(interval, '3 mins')


class PolygonSource(DataSource):
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.polygon.io"

    def fetch(self, symbol: str, days: int, interval: str) -> pd.DataFrame:
        import requests

        multiplier, timespan = self._parse_interval(interval)
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)

        ticker = f"C:{symbol}"
        url = f"{self.base_url}/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{start_date.strftime('%Y-%m-%d')}/{end_date.strftime('%Y-%m-%d')}"

        params = {
            'apiKey': self.api_key,
            'adjusted': 'true',
            'sort': 'asc',
            'limit': 50000
        }

        response = requests.get(url, params=params)
        data = response.json()

        if 'results' not in data:
            return pd.DataFrame()

        df = pd.DataFrame(data['results'])
        df['timestamp'] = pd.to_datetime(df['t'], unit='ms')
        df = df.rename(columns={
            'o': 'open',
            'h': 'high',
            'l': 'low',
            'c': 'close',
            'v': 'volume'
        })
        df = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]
        return df

    def _parse_interval(self, interval: str) -> tuple:
        if interval == '1min':
            return 1, 'minute'
        elif interval == '3min':
            return 3, 'minute'
        elif interval == '5min':
            return 5, 'minute'
        elif interval == '15min':
            return 15, 'minute'
        elif interval == '1hour':
            return 1, 'hour'
        return 3, 'minute'


class BarchartSource(DataSource):
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://marketdata.websol.barchart.com"

    def fetch(self, symbol: str, days: int, interval: str) -> pd.DataFrame:
        import requests

        interval_code = self._convert_interval(interval)
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)

        url = f"{self.base_url}/getHistory.json"
        params = {
            'apikey': self.api_key,
            'symbol': symbol,
            'type': 'minutes',
            'interval': interval_code,
            'startDate': start_date.strftime('%Y%m%d'),
            'endDate': end_date.strftime('%Y%m%d'),
            'order': 'asc'
        }

        response = requests.get(url, params=params)
        data = response.json()

        if 'results' not in data:
            return pd.DataFrame()

        df = pd.DataFrame(data['results'])
        df['timestamp'] = pd.to_datetime(df['tradingDay'] + ' ' + df['timestamp'].str[-8:])
        df = df.rename(columns={
            'open': 'open',
            'high': 'high',
            'low': 'low',
            'close': 'close',
            'volume': 'volume'
        })
        df = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]
        return df

    def _convert_interval(self, interval: str) -> str:
        mapping = {
            '1min': '1',
            '3min': '3',
            '5min': '5',
            '15min': '15',
            '1hour': '60'
        }
        return mapping.get(interval, '3')


class DataFetcher:
    def __init__(self, output_dir: str = '.'):
        self.output_dir = Path(output_dir)

    def fetch_data(
        self,
        source: str,
        symbol: str,
        days: int,
        interval: str,
        api_key: str = None,
        ib_host: str = '127.0.0.1',
        ib_port: int = 7497
    ) -> pd.DataFrame:
        
        # NEVER use sample data - only real data sources
        if source == 'sample':
            raise ValueError(
                "Sample data is not allowed. "
                "Please use TopStep API to fetch data: python fetch_extended_data.py --days 90 --interval 3"
            )
        
        data_source = self._get_source(source, api_key, ib_host, ib_port)

        try:
            df = data_source.fetch(symbol, days, interval)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            print(f"Error fetching from {source}: {e}")

        # NO FALLBACK TO SAMPLE DATA - raise error instead
        raise ValueError(
            f"Failed to fetch data from {source}. "
            "For MGC data, please use TopStep API: python fetch_extended_data.py --days 90 --interval 3"
        )

    def _get_source(self, source: str, api_key: str, ib_host: str, ib_port: int) -> DataSource:
        if source == 'ib':
            return InteractiveBrokersSource(host=ib_host, port=ib_port)
        elif source == 'polygon':
            if not api_key:
                raise ValueError("Polygon requires an API key")
            return PolygonSource(api_key)
        elif source == 'barchart':
            if not api_key:
                raise ValueError("Barchart requires an API key")
            return BarchartSource(api_key)
        else:
            raise ValueError(f"Unknown source: {source}")

    def _interval_to_minutes(self, interval: str) -> int:
        mapping = {'1min': 1, '3min': 3, '5min': 5, '15min': 15, '1hour': 60}
        return mapping.get(interval, 3)

    def save_to_csv(self, df: pd.DataFrame, filename: str = 'data.csv'):
        output_path = self.output_dir / filename
        df.to_csv(output_path, index=False)
        print(f"Data saved to {output_path}")
        return output_path


def main():
    parser = argparse.ArgumentParser(description='Fetch MGC historical data')
    parser.add_argument('--source', type=str, default='topstep',
                        choices=['ib', 'polygon', 'barchart'],
                        help='Data source to use (Note: For MGC, use TopStep API via fetch_extended_data.py)')
    parser.add_argument('--symbol', type=str, default='MGC',
                        help='Futures symbol')
    parser.add_argument('--days', type=int, default=90,
                        help='Number of days of historical data')
    parser.add_argument('--interval', type=str, default='3min',
                        choices=['1min', '3min', '5min', '15min', '1hour'],
                        help='Bar interval')
    parser.add_argument('--api-key', type=str, default=None,
                        help='API key for Polygon or Barchart')
    parser.add_argument('--ib-host', type=str, default='127.0.0.1',
                        help='Interactive Brokers TWS host')
    parser.add_argument('--ib-port', type=int, default=7497,
                        help='Interactive Brokers TWS port')
    parser.add_argument('--output', type=str, default='.',
                        help='Output directory')

    args = parser.parse_args()

    fetcher = DataFetcher(output_dir=args.output)

    # Sample data is not allowed - only real data sources
    if args.source == 'sample':
        print("ERROR: Sample data is not allowed.")
        print("Please use TopStep API to fetch MGC data:")
        print("  python fetch_extended_data.py --days 90 --interval 3")
        return 1
    
    df = fetcher.fetch_data(
        source=args.source,
        symbol=args.symbol,
        days=args.days,
        interval=args.interval,
        api_key=args.api_key,
        ib_host=args.ib_host,
        ib_port=args.ib_port
    )

    if df is not None and not df.empty:
        fetcher.save_to_csv(df)
        print(f"Fetched {len(df)} bars")
    else:
        print("No data fetched")


if __name__ == '__main__':
    main()

