import requests

def search(query):
    headers = {'User-Agent': 'Mozilla/5.0'}
    url = f"https://query2.finance.yahoo.com/v1/finance/search?q={query}"
    res = requests.get(url, headers=headers)
    data = res.json()
    quotes = data.get('quotes', [])
    results = []
    for q in quotes:
        if 'symbol' in q and 'shortname' in q:
            results.append({'symbol': q['symbol'], 'name': q['shortname']})
    return results

print(search("apple"))
