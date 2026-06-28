"""
games_list.py — список игр, которые трекаются.

Это СТАРТОВЫЙ список самых популярных игр Steam + пара Roblox-игр,
чтобы сразу было что показывать. Дальше список расширяется до
"всех игр Steam" через GetAppList — см. README, раздел "Масштабирование".

appid можно проверить/найти на https://store.steampowered.com/app/<appid>
universeId для Roblox — через https://games.roblox.com/v1/games?universeIds=...
(сначала нужно получить universeId по placeId, см. README)
"""

STEAM_GAMES = [
    {"appid": "730", "name": "Counter-Strike 2"},
    {"appid": "570", "name": "Dota 2"},
    {"appid": "578080", "name": "PUBG: BATTLEGROUNDS"},
    {"appid": "1172470", "name": "Apex Legends"},
    {"appid": "271590", "name": "Grand Theft Auto V"},
    {"appid": "1085660", "name": "Destiny 2"},
    {"appid": "252490", "name": "Rust"},
    {"appid": "4000", "name": "Garry's Mod"},
    {"appid": "1245620", "name": "Elden Ring"},
    {"appid": "230410", "name": "Warframe"},
    {"appid": "1091500", "name": "Cyberpunk 2077"},
    {"appid": "359550", "name": "Tom Clancy's Rainbow Six Siege"},
    {"appid": "1599340", "name": "Lost Ark"},
    {"appid": "105600", "name": "Terraria"},
    {"appid": "322330", "name": "Don't Starve Together"},
]

# Roblox: universeId придётся подставить реальные — это пример структуры.
# См. README → "Как добавить Roblox-игру" для пошаговой инструкции.
ROBLOX_GAMES = [
    {"universe_id": "920587237", "name": "Adopt Me!"},
]
