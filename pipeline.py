import pandas as pd
import sqlite3
from datetime import datetime, timedelta
import random


def extract():
    random.seed(42)
    dates = [(datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
             for i in range(90)]
    data = {
        'date': dates,
        'product': [random.choice(['Laptop', 'Phone', 'Tablet']) for _ in dates],
        'quantity': [random.randint(1, 50) for _ in dates],
        'price': [random.uniform(100, 1000) for _ in dates],
        'region': [random.choice(['North', 'South', 'East', 'West']) for _ in dates]
    }
    df = pd.DataFrame(data)
    print(f"Extracted {len(df)} records")
    return df


def transform(df):
    df['date'] = pd.to_datetime(df['date'])
    df['revenue'] = (df['quantity'] * df['price']).round(2)
    df['7day_avg_revenue'] = df['revenue'].rolling(7).mean().round(2)
    df['month'] = df['date'].dt.month
    df = df.dropna()
    return df


def load(df):
    conn = sqlite3.connect('pipeline.db')
    df.to_sql('sales_data', conn, if_exists='replace', index=False)
    conn.close()


def analyze(df):
    conn = sqlite3.connect('pipeline.db')

    print("\n📊 PIPELINE ANALYTICS REPORT")
    print("=" * 40)

    # Total revenue by product
    by_product = df.groupby('product')['revenue'].sum().sort_values(ascending=False)
    print("\n💰 Revenue by Product:")
    print(by_product.round(2))

    # Best performing region
    by_region = df.groupby('region')['revenue'].sum().sort_values(ascending=False)
    print("\n🌍 Revenue by Region:")
    print(by_region.round(2))

    # Monthly trend
    by_month = df.groupby('month')['revenue'].sum()
    print("\n📈 Monthly Revenue Trend:")
    print(by_month.round(2))

    # Peak revenue day
    peak = df.loc[df['revenue'].idxmax()]
    print(f"\n🏆 Peak Revenue Day: {peak['date'].date()} - ${peak['revenue']:,.2f}")

    conn.close()


if __name__ == "__main__":
    print(f"Pipeline started: {datetime.now()}")
    raw = extract()
    cleaned = transform(raw)
    load(cleaned)
    analyze(cleaned)
    print(f"\nPipeline finished: {datetime.now()}")