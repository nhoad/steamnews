#!/usr/bin/python

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

from io import BytesIO

import bbcode

from jinja2 import Environment, Template, DictLoader


log = logging.getLogger('steamnews')


API_DOMAIN = 'api.steampowered.com'
STORE_DOMAIN = 'store.steampowered.com'
GAME_NEWS_URI = '/ISteamNews/GetNewsForApp/v0002/?appid={appid}&count=3&format=json'
APPLIST_URI = '/ISteamApps/GetAppList/v0002/'
APPDETAILS_URI = '/api/appdetails/?appids={appids}'
USER_AGENT = 'steamnews/0.0.1'

EVENT_LOOP = asyncio.get_event_loop()


class HTTPProtocol(asyncio.Protocol):
    domain = API_DOMAIN

    def __init__(self, data, param):
        self.future = asyncio.Future()
        self.request_data = data
        self.param = param

    def connection_made(self, transport):
        self.buf = BytesIO()
        self.transport = transport
        self.transport.write(self.request_data)

    def data_received(self, data):
        self.buf.write(data)

    def connection_lost(self, exc):
        response = self.buf.getvalue()

        data = self.decode_response(response)

    @classmethod
    def get(cls, param):
        params = cls.get_params(param)
        # we cheat with HTTP/1.0 to avoid handling chunked decoding. I'm sorry.
        data = '''GET {uri} HTTP/1.0\r
Host: {domain}\r
User-Agent: {user_agent}\r
Connection: close\r
\r
'''.format(domain=cls.domain, user_agent=USER_AGENT, **params)

        request = cls(data.encode('utf8'), param)
        asyncio.async(EVENT_LOOP.create_connection(lambda: request, host=cls.addr(), port=80))

        return request.future

    def decode_response(self, response):
        try:
            _headers, body = response.split(b'\r\n\r\n', 1)
        except ValueError as e:
            log.exception('Error splitting HTTP response %r', response)
            self.future.set_exception(e)
        else:
            try:
                data = json.loads(body.decode('utf8'))
            except ValueError as e:
                log.exception("Error parsing JSON response %r for request %r", response, self.request_data)
                self.future.set_exception(e)
            else:
                data = self.final_decoding(data)
                self.future.set_result(data)

    def final_decoding(self, data):
        return data

    @classmethod
    def addr(cls):
        return API_ADDR


class AppList(HTTPProtocol):
    @classmethod
    def get_params(cls, param):
        return dict(uri=APPLIST_URI)

    def final_decoding(self, json):
        return json['applist']['apps']


class GameNews(HTTPProtocol):
    @classmethod
    def get_params(cls, param):
        return dict(uri=GAME_NEWS_URI.format(appid=param))


class AppDetails(HTTPProtocol):
    domain = STORE_DOMAIN

    @classmethod
    def addr(cls):
        return STORE_ADDR

    @classmethod
    def get_params(cls, param):
        assert not isinstance(param, list)
        param = str(param)
        return dict(uri=APPDETAILS_URI.format(appids=param))


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
        news = yield from GameNews.get(self.appid)
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


class GameDB:
    # trim out things that we know, in our heart of hearts, are garbage, that
    # no one wants to know about.
    # Also, this is crazy gross. I'm sorry.
    USELESS = re.compile(r'(.*(\?\?\?|ValveTestApp|Game Key| E3 |DLC|Dedicated [Ss]erver|_|Soundtrack|Pre\-[Oo]rder|Teaser|[tT]railer|Trailer \d|Add\-On|CD Key).*|(.*(Gameplay|Preview|Review|Pack|Strategy Guide|Development Kit|Foil Conversion|Foil|Deck Key|Demo)$))').match

    def __init__(self):
        should_create = not os.path.exists('games.db')
        db = sqlite3.connect('games.db')

        if should_create:
            cur = db.cursor()
            cur.execute('CREATE TABLE games (appid integer primary key, name varchar, type varchar, windows boolean, mac boolean, linux boolean, early_access boolean)')
            db.commit()

        self.db = db

    @asyncio.coroutine
    def update(self, games):

        c = self.db.cursor()
        known_ids = set(f[0] for f in c.execute('SELECT appid FROM games').fetchall())

        unknown_games = (g for g in games if int(g['appid']) not in known_ids)
        unknown_games = [g for g in unknown_games if not self.USELESS(g['name'])]

        if unknown_games:
            log.info('Will check out {} unknown games.'.format(len(unknown_games)))
            s = time.time()
            unknown_games = yield from self.filter_out_garbage(unknown_games)
            e = time.time()
            log.info('took %.2f seconds' % (e - s))
        else:
            log.info("No new games to check out (we know about all %s)", len(known_ids))

        self.games = self.get_all()

    @asyncio.coroutine
    def filter_out_garbage(self, games):
        chunk_size = 20

        games_map = {game['appid']: game['name'] for game in games}

        app_details_futures = asyncio.as_completed([
            asyncio.async(AppDetails.get(game['appid']))
            for game in games
        ])

        cursor = self.db.cursor()

        for future in app_details_futures:
            try:
                info = yield from future
            except ValueError as e:
                continue

            apps = []

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
                log.debug('appid=%s name=%s game_type=%s windows=%s mac=%s linux=%s early_acess=%s' % f)
                apps.append(f)

            cursor.executemany('INSERT INTO games (appid, name, type, windows, mac, linux, early_access) VALUES (?, ?, ?, ?, ?, ?, ?)', apps)
            self.db.commit()

    def get_all(self):
        c = self.db.cursor()
        games = c.execute("SELECT appid, name, windows, mac, linux, early_access FROM games where type == 'game'").fetchall()
        return [Game(*g) for g in games]

    def write_frontend(self):
        with open('index.html.template') as f:
            template = f.read()

        with open('steamnews/index.html.tmp', 'w') as f:
            # gross, I know.
            f.write(template.replace('INSERT_GAMES_HERE', json.dumps([g.as_slim_dict() for g in self.games])))

        # hack to make it atomic.
        os.rename('steamnews/index.html.tmp', 'steamnews/index.html')

    @asyncio.coroutine
    def get_news(self):
        games = self.games

        xml_renderer = AtomRenderer()
        chunk_size = 500
        total = len(games)

        games = [g for g in games if g.needs_update()]
        log.info("Out of %d games, %d need updating", total, len(games))

        chunked_games = [games[i:i + chunk_size] for i in range(0, len(games), chunk_size)]

        for games in chunked_games:
            s = time.time()
            news_futures = [asyncio.async(game.get_news()) for game in games]

            for future in asyncio.as_completed(news_futures):
                game = yield from future

                if game:
                    with open('steamnews/%s.json' % game['appid'], 'w') as f:
                        json.dump(game, f)

                    with open('steamnews/%s.atom.tmp' % game['appid'], 'w') as f:
                        f.write(xml_renderer(game))

                    # hack to make it atomic.
                    os.rename('steamnews/%s.atom.tmp' % game['appid'], 'steamnews/%s.atom' % game['appid'])

            total -= len(games)
            log.info('Completed %d items in %.2f seconds (%d remaining)' % (len(games), time.time()-s, total))


@asyncio.coroutine
def main(timeout):
    global API_ADDR, STORE_ADDR

    API_ADDR = (yield from EVENT_LOOP.getaddrinfo(API_DOMAIN, 80))[0][-1][0]
    STORE_ADDR = (yield from EVENT_LOOP.getaddrinfo(STORE_DOMAIN, 80))[0][-1][0]

    log.info("Retrieving list of games on Steam")

    # dummy required parameter :(
    games = yield from AppList.get(None)

    log.debug("done")

    db = GameDB()

    yield from db.update(games)

    log.info("Retrieving news for games")
    yield from db.get_news()

    log.debug("done")

    log.info("Writing out frontend")
    db.write_frontend()
    log.debug("done")

    timeout.set_result(True)

if __name__ == '__main__':
    logging.basicConfig(
        filename='steamnews.log',
        level=logging.DEBUG if 'debug' in sys.argv else logging.INFO,
        format='[%(asctime)s] %(levelname)s %(message)s'
    )
    logging.getLogger('asyncio').setLevel(logging.INFO)

    log.info('Starting')

    log.info("Pushing fd limits higher")
    resource.setrlimit(resource.RLIMIT_NOFILE, (50000, 50000))

    f = asyncio.Future()

    def timeout(future):
        log.info('Shutting down after 10 minutes')
        future.set_result(True)

    EVENT_LOOP.call_later(60*10, timeout, f)
    asyncio.async(main(f))
    EVENT_LOOP.run_until_complete(f)

    log.info('Finished')
