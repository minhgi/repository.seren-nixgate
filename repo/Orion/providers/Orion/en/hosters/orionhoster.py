# -*- coding: utf-8 -*-

try: # Python 2
	from urllib import urlencode
	from urlparse import parse_qs
except: # Python 3
	from urllib.parse import urlencode, parse_qs

from ...orionoid import Orionoid

class source: # source, not sources.

	def __init__(self):
		self.orion = Orionoid.instance()

	def movie(self, imdb, title, localtitle, aliases, year):
		try:
			url = {'imdb' : imdb, 'title' : title, 'year' : year}
			return urlencode(url)
		except: return None

	def tvshow(self, imdb, tvdb, tvshowtitle, localtvshowtitle, aliases, year):
		try:
			url = {'imdb' : imdb, 'tvdb' : tvdb, 'tvshowtitle' : tvshowtitle, 'year' : year}
			return urlencode(url)
		except: return None

	def episode(self, url, imdb, tvdb, title, premiered, season, episode):
		try:
			if not url: return
			url = parse_qs(url)
			url = dict([(i, url[i][0]) if url[i] else (i, '') for i in url])
			url['title'], url['premiered'], url['season'], url['episode'] = title, premiered, season, episode
			return urlencode(url)
		except: return None

	def sources(self, url, hostDict, hostprDict):
		try:
			data = parse_qs(url)
			data = dict([(i, data[i][0]) if data[i] else (i, '') for i in data])
			try: imdb = data['imdb']
			except: imdb = None
			try: tvdb = data['tvdb']
			except: tvdb = None
			if 'tvshowtitle' in data: return Orionoid.streams(type = self.orion.TypeShow, stream = self.orion.StreamHoster, imdb = imdb, tvdb = tvdb, season = data['season'], episode = data['episode'], count = None)
			else: return Orionoid.streams(type = self.orion.TypeMovie, stream = self.orion.StreamHoster, imdb = imdb, tvdb = tvdb)
		except:
			Orionoid.error()
			return None
