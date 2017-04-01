import os
import datetime
import json
import re
import time
import logging

import click


USELESS = re.compile(r'(.*(\?\?\?|ValveTestApp|Game Key| E3 |DLC|Dedicated [Ss]erver|_|Soundtrack|Pre\-[Oo]rder|Teaser|[tT]railer|Bleach|Naruto Shippuden Uncut|Fantasy Grounds - |Rocksmith.? (2014 (Edition )?)?. |Inside The Walking Dead|Trailer \d|Add\-On|CD Key).*|(.*(Gameplay|Preview|Review|Pack|Strategy Guide|Development Kit|Trailer|Skin|Foil Conversion|Foil|Deck Key|Demo|OST)$))').match


log = logging.getLogger('steamnews')


@click.group()
@click.option('--debug', is_flag=True, default=False)
def steamnews(debug):
    logging.basicConfig(
        filename='steamnews.log',
        level=logging.DEBUG if debug else logging.INFO,
        format='[%(asctime)s] %(levelname)s %(message)s'
    )
    for d in ['news', 'games', 'templates']:
        try:
            os.mkdir(d)
        except FileExistsError:
            continue


@steamnews.command()
def serve():
    import gevent
    import gevent.monkey
    import gevent.wsgi
    gevent.monkey.patch_all()

    import flask
    app = flask.Flask('steamnews')

    @app.route('/')
    def index():
        return flask.render_template('index.html')


    @app.route('/<int:appid>.atom')
    def atom(appid):
        try:
            with open('news/{}.atom'.format(appid)) as f:
                return flask.Response(f.read(), mimetype='application/rss+xml')
        except FileNotFoundError:
            renderer = AtomRenderer()
            update_game_news(appid, renderer, mode='x')
            return atom(appid)

    http_server = gevent.wsgi.WSGIServer(('127.0.0.1', 5000), app)
    http_server.serve_forever()


@steamnews.command()
def ignored():
    import requests

    with open('permanently-ignored.json') as f:
        ignore_list = json.load(f)

    r = requests.get(
        'http://api.steampowered.com/ISteamApps/GetAppList/v0002/')
    app_list = r.json()
    app_list = app_list['applist']['apps']
    app_map = {int(app['appid']): app['name'] for app in app_list}

    for ignored in sorted(ignore_list):
        click.echo("{} {}".format(ignored, app_map[ignored]))


@steamnews.command()
def update():
    # basic steps:
    # - download list of all apps, filter out the obvious garbage
    # - download list of info for apps we don't know about, or if app
    #   information is more than 24 hours old
    # - save to index.html
    # - for all games people want to know about, update the news

    update_front_page()

    # looks weird, but we're trying to iterate over all things people want news
    # for, and then download the news for it according to the json in games/.
    renderer = AtomRenderer()

    for atom in os.listdir('news'):
        appid = os.path.splitext(atom)[0]
        update_game_news(appid, renderer, mode='w')


def update_game_news(appid, renderer, mode):
    import requests

    with open('games/{}.json'.format(appid)) as f:
        game_info = json.load(f)

    url = 'http://api.steampowered.com/ISteamNews/GetNewsForApp/v0002/?appid={appid}&count=3&format=json'.format(appid=appid)
    resp = requests.get(url)
    news = resp.json()

    newsitems = news.get('appnews', {}).get('newsitems', [])
    game_info['newsitems'] = newsitems

    with open('news/{}.atom'.format(appid), mode) as f:
        f.write(renderer(game_info))


def update_front_page():
    import requests

    r = requests.get(
        'http://api.steampowered.com/ISteamApps/GetAppList/v0002/')
    app_list = r.json()
    app_list = app_list['applist']['apps']
    app_list = [g for g in app_list if not USELESS(g['name'])]

    try:
        with open('permanently-ignored.json') as f:
            ignore_list = json.load(f)
    except Exception:
        ignore_list = []

    def update_apps():
        count = 0
        for i, app in enumerate(app_list, 1):
            appid = int(app['appid'])
            if appid in ignore_list:
                continue

            # only check games that are older than 24 hours
            try:
                with open('games/{}.json'.format(appid)) as f:
                    age = json.load(f)['lookup_time']
            except Exception as e:
                log.info("%s: no local data available, forced to retrieve", appid)
            else:
                one_day = 60 * 60 * 24
                if age > time.time() - one_day:
                    log.info("%s: too young, skipping", appid)
                    continue

            uri = 'http://store.steampowered.com/api/appdetails/?appids={}'.format(app['appid'])
            r = requests.get(uri)
            count += 1
            if r.status_code == 429:
                log.info("Exceeded the rate limit after %s requests (%s out of %s)", count, i, len(app_list))
                return

            game_info = r.json()[str(appid)]

            # region blocked, we should ignore it forever
            success = game_info.get('success', {})
            if not success:
                log.info("%s: couldn't successfully get %r, marking as permanently ignored", appid, app['name'])
                ignore_list.append(appid)
                continue

            game_data = game_info.get('data', {})
            game_type = game_data.get('type', "UNKNOWN!!")
            if game_type != 'game':
                ignore_list.append(appid)
                continue

            early_access = 70 in set(int(m['id']) for m in game_data.get('genres', []))
            platforms = game_data.get('platforms', {})

            windows = platforms.get('windows', False)
            mac = platforms.get('mac', False)
            linux = platforms.get('linux', False)

            game = {
                'appid': appid,
                'name': app['name'],
                'windows': windows,
                'mac': mac,
                'linux': linux,
                'early_access': early_access,
                'lookup_time': int(time.time()),
            }

            with open('games/{}.json'.format(appid), 'w') as f:
                json.dump(game, f)

    try:
        update_apps()
    finally:
        with open('permanently-ignored.json', 'w') as f:
            json.dump(ignore_list, f)

    log.info("Writing frontend")

    with open('index.html.template') as f:
        template = f.read()

    games = [
        json.load(open(os.path.join('games', p)))
        for p in os.listdir('games')]

    with open('index.html.tmp', 'w') as f:
        # gross, I know.
        f.write(template.replace('INSERT_GAMES_HERE', json.dumps(games)))

    # hack to make it atomic.
    os.rename('index.html.tmp', 'templates/index.html')

    log.info("Run complete")


class AtomRenderer:
    def __init__(self):
        import bbcode
        import jinja2

        with open('atomfeed.xml') as f:
            atom_template = f.read()

        env = jinja2.Environment()
        env.loader = jinja2.DictLoader({'atom': atom_template})
        env.filters['article'] = self.render_article
        env.filters['isodate'] = self.isodate

        self.env = env

        self.bbcode_parser = bbcode.Parser(escape_html=False, replace_links=False)
        self.bbcode_parser.add_simple_formatter('img', '<img src="%(value)s">')

        for i in range(1, 7):
            tag = 'h%d' % i
            self.bbcode_parser.add_simple_formatter(tag, '<{t}>%(value)s</{t}>'.format(t=tag))

    def render_article(self, value):
        import cgi
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


if __name__ == '__main__':
    steamnews()
