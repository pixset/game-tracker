"""
games_list.py — стартовый список игр.

Steam: appid — число из URL store.steampowered.com/app/<appid>/...
Roblox: place_id — число из URL roblox.com/games/<place_id>/...
        (это НЕ universeId — backend сам конвертирует при старте через
         apis.roblox.com/universes/v1/places/<place_id>/universe)
Minecraft: address — хост сервера (то, что игрок вводит в "Добавить сервер"
           в самом Minecraft). Онлайн берётся через публичный пинг-статус
           api.mcsrvstat.us — ключей и регистрации не требует.
"""

STEAM_GAMES = [
    {"appid": "730",     "name": "Counter-Strike 2"},
    {"appid": "570",     "name": "Dota 2"},
    {"appid": "578080",  "name": "PUBG: BATTLEGROUNDS"},
    {"appid": "1172470", "name": "Apex Legends"},
    {"appid": "271590",  "name": "Grand Theft Auto V"},
    {"appid": "1085660", "name": "Destiny 2"},
    {"appid": "252490",  "name": "Rust"},
    {"appid": "4000",    "name": "Garry's Mod"},
    {"appid": "1245620", "name": "Elden Ring"},
    {"appid": "230410",  "name": "Warframe"},
    {"appid": "1091500", "name": "Cyberpunk 2077"},
    {"appid": "359550",  "name": "Tom Clancy's Rainbow Six Siege"},
    {"appid": "1599340", "name": "Lost Ark"},
    {"appid": "105600",  "name": "Terraria"},
    {"appid": "322330",  "name": "Don't Starve Together"},
    {"appid": "440",     "name": "Team Fortress 2"},
    {"appid": "550",     "name": "Left 4 Dead 2"},
    {"appid": "489830",  "name": "The Elder Scrolls V: Skyrim SE"},
    {"appid": "1174180", "name": "Red Dead Redemption 2"},
    {"appid": "1517290", "name": "Battlefield 2042"},
]

# place_id берётся из URL roblox.com/games/<place_id>/...
# backend сам конвертирует в universeId при старте
ROBLOX_GAMES = [
    {"place_id": "2753915549",  "name": "Blox Fruits"},
    {"place_id": "4924922222",  "name": "Brookhaven RP"},
    {"place_id": "920587237",   "name": "Adopt Me!"},
    {"place_id": "6284583030",  "name": "Pet Simulator X"},
    {"place_id": "606849621",   "name": "Jailbreak"},
    {"place_id": "142823291",   "name": "Murder Mystery 2"},
    {"place_id": "370731277",   "name": "MeepCity"},
    {"place_id": "1962086868",  "name": "Tower of Hell"},
    {"place_id": "286090429",   "name": "Arsenal"},
    {"place_id": "6872265039",  "name": "BedWars"},
    {"place_id": "6381829480",  "name": "Dress to Impress"},
    {"place_id": "1537690962",  "name": "Bee Swarm Simulator"},
]

# address — хост сервера, тот же, что вводится в клиенте Minecraft.
#
# ВАЖНО про эту подборку: адреса собраны по открытым источникам (обзоры,
# серверные листинги) — я не могу лично пропинговать их из этой песочницы
# (сеть здесь режется белым списком доменов, mcsrvstat.us и Minecraft-порты
# в него не входят), поэтому 100% "прямо сейчас у всех не ноль" гарантировать
# не могу. Если после деплоя какой-то сервер стабильно показывает 0 или
# вообще не появляется в списке (адрес мог смениться/сервер закрылся) —
# просто замени/удали строку ниже, никаких других правок не нужно.
# The Hive (play.hivemc.com) убран: его Java-адрес фактически заброшен —
# игроки давно перетекли на Bedrock-версию (geo.hivebedrock.network), которая
# работает по другому протоколу и здесь не поддерживается.
MINECRAFT_SERVERS = [
    {"address": "hypixel.net",           "name": "Hypixel"},
    {"address": "us.mineplex.com",       "name": "Mineplex"},
    {"address": "play.cubecraft.net",    "name": "CubeCraft Games"},
    {"address": "play.wynncraft.com",    "name": "Wynncraft"},
    {"address": "2b2t.org",              "name": "2b2t"},
    {"address": "play.manacube.com",     "name": "ManaCube"},
    {"address": "hub.mc-complex.com",    "name": "Complex Gaming"},
    {"address": "play.pika.host",        "name": "PikaNetwork"},
    {"address": "play.purpleprison.org", "name": "Purple Prison"},
    {"address": "play.minesuperior.com", "name": "MineSuperior"},
    {"address": "massivecraft.com",      "name": "MassiveCraft"},
    {"address": "mc.advancius.net",      "name": "Advancius Network"},
    {"address": "play.munchymc.com",     "name": "MunchyMC"},
    {"address": "play.blocksmc.com",     "name": "BlocksMC"},
    {"address": "play.cosmicpvp.com",    "name": "CosmicPvP"},
    {"address": "us.minemen.club",       "name": "Minemen Club"},
    {"address": "play.invadedlands.net", "name": "InvadedLands"},
    {"address": "play.pokefind.co",      "name": "PokeFind"},
    {"address": "play.minemalia.com",    "name": "MineMalia"},
    {"address": "the-gommehd.net",       "name": "GommeHD"},
    {"address": "earthmc.net",           "name": "EarthMC"},
    {"address": "mc.brawl.com",          "name": "Brawl"},
    {"address": "sl.extremecraft.net",   "name": "ExtremeCraft"},
    {"address": "sm.insanitycraft.net",  "name": "InsanityCraft"},
    {"address": "play.jartexnetwork.com","name": "JartexNetwork"},
]
