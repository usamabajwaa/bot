import pandas as pd

df = pd.read_csv('data.csv')
df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
dates = sorted(df['timestamp'].dt.date.unique())

print('Last 5 dates with data:')
for d in dates[-5:]:
    day_data = df[df['timestamp'].dt.date == d]
    print(f'  {d}: {len(day_data)} bars (time range: {day_data["timestamp"].min().time()} to {day_data["timestamp"].max().time()})')

