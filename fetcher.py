#!/usr/bin/python

import aiohttp
import asyncio
import cgi
import datetime
import json
import logging
import os
import re
import resource
import sqlite3
import sys
import time

import bbcode

from jinja2 import Environment, Template, DictLoader


log = logging.getLogger('steamnews')

EVENT_LOOP = asyncio.get_event_loop()

# trim out things that we know, in our heart of hearts, are garbage, that
# no one wants to know about.
# Also, this is crazy gross. I'm sorry.
USELESS = re.compile(r'(.*(\?\?\?|ValveTestApp|Game Key| E3 |DLC|Dedicated [Ss]erver|_|Soundtrack|Pre\-[Oo]rder|Teaser|[tT]railer|Trailer \d|Add\-On|CD Key).*|(.*(Gameplay|Preview|Review|Pack|Strategy Guide|Development Kit|Foil Conversion|Foil|Deck Key|Demo)$))').match

MAX_GAME_LOOKUPS = 200


CONN_POOL = aiohttp.TCPConnector()


@asyncio.coroutine
def get_app_list():
    r = yield from aiohttp.request(
        'GET', 'http://api.steampowered.com/ISteamApps/GetAppList/v0002/',
        connector=CONN_POOL)
    app_list = yield from r.json()
    app_list = app_list['applist']['apps']
    return app_list


@asyncio.coroutine
def check_out_unknown_games(games, db):
    games = [g for g in games if not USELESS(g['name'])]

    cursor = db.cursor()

    known_ids = set(f[0] for f in cursor.execute('SELECT appid FROM games').fetchall())

    unknown_games = [g for g in games if int(g['appid']) not in known_ids][:MAX_GAME_LOOKUPS]

    if unknown_games:
        log.info('Will check out {} unknown games (out of {}, {} done so far).'.format(len(unknown_games), len(games), len(known_ids)))
        s = time.time()
        unknown_games = yield from lookup_games(unknown_games, db)
        e = time.time()
        log.info('took %.2f seconds' % (e - s))
    else:
        log.info("No new games to check out (we know about all %s)", len(known_ids))


@asyncio.coroutine
def update_game_news(db):
    cursor = db.cursor()
    games = get_games(db)
    games = [g for g in games if g.needs_update()]

    xml_renderer = AtomRenderer()

    game_futures = asyncio.as_completed([
        asyncio.async(game.get_news())
        for game in games
    ])

    for future in game_futures:
        game = yield from future

        if game:
            with open('steamnews/%s.json' % game['appid'], 'w') as f:
                json.dump(game, f, indent=4)

            with open('steamnews/%s.atom.tmp' % game['appid'], 'w') as f:
                f.write(xml_renderer(game))

            # hack to make it atomic.
            os.rename('steamnews/%s.atom.tmp' % game['appid'], 'steamnews/%s.atom' % game['appid'])


def write_frontend(db):
    games = get_games(db)

    with open('index.html.template') as f:
        template = f.read()

    with open('steamnews/index.html.tmp', 'w') as f:
        # gross, I know.
        f.write(template.replace('INSERT_GAMES_HERE', json.dumps([g.as_slim_dict() for g in games])))

    # hack to make it atomic.
    os.rename('steamnews/index.html.tmp', 'steamnews/index.html')


@asyncio.coroutine
def lookup_games(games, db):
    cursor = db.cursor()

    games_map = {game['appid']: game['name'] for game in games}

    apps = []

    game_futures = [
        (game, asyncio.async(get_game_details(game['appid'])))
        for game in games
    ]

    for game, future in game_futures:
        try:
            info = yield from future
        except ValueError as e:
            log.info("Error getting details for %s", game['appid'])
            continue

        for appid, game_info in info.items():
            appid = int(appid)
            name = games_map[appid]
            game_data = game_info.get('data', {})
            game_type = game_data.get('type', "UNKNOWN!!")
            early_access = 70 in set(int(m['id']) for m in game_data.get('genres', []))
            platforms = game_data.get('platforms', {})

            windows = platforms.get('windows', False)
            mac = platforms.get('mac', False)
            linux = platforms.get('linux', False)
            f = (appid, name, game_type, windows, mac, linux, early_access)
            log.debug('appid=%s name=%s game_type=%s windows=%s mac=%s linux=%s early_access=%s' % f)
            apps.append(f)

    cursor.executemany('INSERT INTO games (appid, name, type, windows, mac, linux, early_access) VALUES (?, ?, ?, ?, ?, ?, ?)', apps)
    db.commit()


def get_games(db):
    cursor = db.cursor()
    games = cursor.execute("SELECT appid, name, windows, mac, linux, early_access FROM games where type == 'game'").fetchall()
    games = [Game(*g) for g in games]
    return games


class Game:
    def __init__(self, appid, name, windows, mac, linux, early_access):
        self.appid = int(appid)
        self.name = name
        self.newsitems = []
        self.windows = windows
        self.mac = mac
        self.linux = linux
        self.early_access = early_access

    def needs_update(self):
        try:
            # to avoid server errors breaking feeds for too long, we actually
            # run this job every 15 minutes, but we want to avoid hitting the
            # server if we've modified the file in the past hour.
            if os.path.getmtime('steamnews/%s.atom' % self.appid) > (time.time() - (60*60)):
                return False
        except FileNotFoundError:
            pass
        return True

    @asyncio.coroutine
    def get_news(self):
        uri = 'http://api.steampowered.com/ISteamNews/GetNewsForApp/v0002/?appid={appid}&count=3&format=json'.format(appid=self.appid)
        r = yield from aiohttp.request('GET', uri, connector=CONN_POOL)
        news = yield from r.json()
        self.newsitems = news.get('appnews', {}).get('newsitems', [])
        return self.as_dict()

    def as_slim_dict(self):
        return {
            'appid': self.appid,
            'name': self.name,
            'windows': self.windows,
            'mac': self.mac,
            'linux': self.linux,
            'early_access': self.early_access,
        }

    def as_dict(self):
        return {
            'appid': self.appid,
            'name': self.name,
            'newsitems': self.newsitems,
            'updatetime': time.time(),
            'windows': self.windows,
            'mac': self.mac,
            'linux': self.linux,
            'early_access': self.early_access,
        }


class AtomRenderer:
    def __init__(self):
        with open('atomfeed.xml') as f:
            atom_template = f.read()

        env = Environment()
        env.loader = DictLoader({'atom': atom_template})
        env.filters['article'] = self.render_article
        env.filters['isodate'] = self.isodate

        self.env = env

        self.bbcode_parser = bbcode.Parser(escape_html=False, replace_links=False)
        self.bbcode_parser.add_simple_formatter('img', '<img src="%(value)s">')

        for i in range(1, 7):
            tag = 'h%d' % i
            self.bbcode_parser.add_simple_formatter(tag, '<{t}>%(value)s</{t}>'.format(t=tag))

    def render_article(self, value):
        for k in self.bbcode_parser.recognized_tags.keys():

            # don't check closing bracket for [url=...] tags.
            k = '[%s' % k

            # handle upper case tags
            if k in value.lower():
                return cgi.escape(self.bbcode_parser.format(value))

        return cgi.escape(value)  # assumed HTML

    def isodate(self, value):
        return datetime.datetime.fromtimestamp(value).isoformat()

    def __call__(self, game):
        return self.env.get_template('atom').render(game)


def open_db():
    should_create = not os.path.exists('games.db')
    db = sqlite3.connect('games.db')

    if should_create:
        cur = db.cursor()
        cur.execute('CREATE TABLE games (appid integer primary key, name varchar, type varchar, windows boolean, mac boolean, linux boolean, early_access boolean)')
        db.commit()
    return db


@asyncio.coroutine
def get_game_details(gameid):
    uri = 'http://store.steampowered.com/api/appdetails/?appids={}'.format(gameid)
    r = yield from aiohttp.request('GET', uri, connector=CONN_POOL)
    json = yield from r.json()
    if r.status == 429:
        raise ValueError("Exceeded the rate limit!!")
    return json


@asyncio.coroutine
def main():
    app_list = yield from get_app_list()

    with open_db() as db:
        yield from check_out_unknown_games(app_list, db)
        yield from update_game_news(db)

        write_frontend(db)


if __name__ == '__main__':
    logging.basicConfig(
        filename='steamnews.log',
        level=logging.DEBUG if 'debug' in sys.argv else logging.INFO,
        format='[%(asctime)s] %(levelname)s %(message)s'
    )
    logging.getLogger('asyncio').setLevel(logging.INFO)

    log.info('Starting')

    log.info("Pushing fd limits higher")
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (50000, 50000))
    except ValueError:
        pass

    EVENT_LOOP.run_until_complete(main())

    log.info('Finished')
