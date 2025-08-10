import pandas as pd
import pdfplumber

def parse(pdf_path: str) -> pd.DataFrame:
    tables = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                tables.append(table)
    df = pd.DataFrame([row for table in tables for row in table[1:]])
    df.columns = [col.strip() for col in tables[0][0]]
    df = df.apply(lambda x: x.str.strip() if x.dtype == 'object' else x)
    df = df.apply(pd.to_numeric, errors='ignore')
    df = df.apply(pd.to_datetime, errors='ignore')
    return df