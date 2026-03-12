"""Quick check: do frxeur and ukxgbp exist on HistData.com?"""
import requests

pairs = ['frxeur', 'ukxgbp', 'spxusd']  # spxusd as control
for pair in pairs:
    url = f'https://www.histdata.com/download-free-forex-historical-data/?/ascii/1-minute-bar-quotes/{pair}/2023'
    try:
        r = requests.get(url, timeout=15)
        has_token = 'id="tk"' in r.text
        has_download = 'download' in r.text.lower()
        print(f'{pair}: status={r.status_code}, has_download_token={has_token}, page_len={len(r.text)}')
    except Exception as e:
        print(f'{pair}: ERROR - {e}')
