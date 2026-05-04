import json

with open('cards.json', encoding='utf-8') as f:
    data = json.load(f)

filtered = []
for c in data:
    if c['cardRarityType'] in ['rarity_3', 'rarity_4']:
        filtered.append({
            'id': c['id'],
            'characterId': c['characterId'],
            'cardRarityType': c['cardRarityType'],
            'assetbundleName': c['assetbundleName']
        })

with open('cards_filtered.json', 'w', encoding='utf-8') as f:
    json.dump(filtered, f, ensure_ascii=False, indent=4)

print(f'原始数量: {len(data)}')
print(f'过滤后数量: {len(filtered)}')
print(f'rarity_3: {sum(1 for c in data if c["cardRarityType"] == "rarity_3")}')
print(f'rarity_4: {sum(1 for c in data if c["cardRarityType"] == "rarity_4")}')
