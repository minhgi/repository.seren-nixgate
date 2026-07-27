# -*- coding: utf-8 -*-

from ...orionoid import Orionoid

class sources:

	def __init__(self):
		self.orion = Orionoid.instance()

	def movie(self, title, year, imdb = None, **kwarg):
		return Orionoid.streams(type = self.orion.TypeMovie, stream = self.orion.StreamTorrent, imdb = imdb)

	def episode(self, simple_info, all_info, **kwargs):
		if 'showInfo' in all_info: # Old Seren
			try: imdb = all_info['showInfo']['info']['imdbnumber']
			except:
				try: imdb = all_info['showInfo']['trakt_object']['shows'][0]['ids']['imdb']
				except: imdb = all_info['showInfo']['ids']['imdb']
			try: count = int(all_info['showInfo']['info']['episode_count']) / float(all_info['showInfo']['info']['season_count'])
			except:
				try: count = int(all_info['showInfo']['info']['episodeCount']) / float(all_info['showInfo']['info']['seasonCount'])
				except: count = None
			try: season = all_info['info']['season']
			except: season = all_info['episodeInfo']['info']['season']
			try: episode = all_info['info']['episode']
			except: episode = all_info['episodeInfo']['info']['episode']
			return Orionoid.streams(type = self.orion.TypeShow, stream = self.orion.StreamTorrent, imdb = imdb, season = season, episode = episode, count = count)
		else: # New Seren
			imdb = None
			tvdb = None
			tmdb = None
			trakt = None

			try: imdb = all_info['info']['tvshow.imdb_id']
			except:
				try: imdb = all_info['info']['imdb_show_id']
				except: pass

			if not imdb:
				try: tvdb = all_info['info']['tvshow.tvdb_id']
				except:
					try: tvdb = all_info['info']['tvdb_show_id']
					except: pass

				if not tvdb:
					try: tmdb = all_info['info']['tvshow.tmdb_id']
					except:
						try: tmdb = all_info['info']['tmdb_show_id']
						except: pass

					if not tmdb:
						try: trakt = all_info['info']['tvshow.trakt_id']
						except:
							try: trakt = all_info['info']['trakt_show_id']
							except: pass

			try: count = int(all_info['episode_count'])
			except:
				try: count = int(all_info['show_episode_count']) / float(all_info['season_count'])
				except: count = None

			season = all_info['info']['season']
			episode = all_info['info']['episode']

			return Orionoid.streams(type = self.orion.TypeShow, stream = self.orion.StreamTorrent, imdb = imdb, tvdb = tvdb, tmdb = tmdb, trakt = trakt, season = season, episode = episode, count = count)
