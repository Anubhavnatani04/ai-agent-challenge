import pandas as pd
import pytest
from custom_parsers.icici_parser import parse

def test_icici_parse():
    pdf_path = 'data/icici/icici sample.pdf'
    csv_path = 'data/icici/result.csv'
    df = parse(pdf_path)
    expected_df = pd.read_csv(csv_path)
    expected_df = expected_df.apply(lambda x: x.str.strip() if x.dtype == 'object' else x)
    expected_df = expected_df.apply(pd.to_numeric, errors='ignore')
    expected_df = expected_df.apply(pd.to_datetime, errors='ignore')
    assert df.equals(expected_df)