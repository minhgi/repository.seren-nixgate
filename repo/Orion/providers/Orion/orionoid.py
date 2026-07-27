# -*- coding: utf-8 -*-

import sys
import base64
import traceback
import xbmc
import xbmcvfs

class Orionoid(object):

	Instance = None
	Key = 'VW1sQ1UwbEdSV2RTUTBKRlNVVkpaMDlUUWs1SlJYTm5UbWxDUjBsRk1HZFZVMEpMU1VSaloxSnBRVFZKUmtWblZGTkJNa2xGZDJkT2VVSkpTVVZuWjFONVFsaEpSVEJuVVhsQ1JVbEVUV2RYUTBKQw=='
	Location = 'special://home/addons/script.module.orion/lib'

	@classmethod
	def log(self, message, name = True, parameters = None):
		message = str(message)
		if name:
			nameValue = 'Orion'
			if not name == True: nameValue += ' ' + name
			if parameters:
				nameValue += ' ['
				try: nameValue += parameters
				except: nameValue += ', '.join([str(parameter) for parameter in parameters])
				nameValue += ']'
			nameValue += ': '
			message = nameValue + message
		xbmc.log(message, xbmc.LOGERROR)

	@classmethod
	def error(self, message = None, exception = True):
		if exception:
			type, value, traceback = sys.exc_info()
			filename = traceback.tb_frame.f_code.co_filename
			linenumber = traceback.tb_lineno
			name = traceback.tb_frame.f_code.co_name
			errortype = type.__name__
			try: errormessage = value.message
			except: errormessage = str(value)
			if message: message += ' -> '
			else: message = ''
			message += str(errortype) + ' -> ' + str(errormessage)
			parameters = [filename, linenumber, name]
		else:
			parameters = None
		self.log(message = message, name = 'ERROR', parameters = parameters)

	@classmethod
	def instance(self):
		if Orionoid.Instance is None:
			path = Orionoid.Location
			try: path = xbmcvfs.translatePath(path) # New Kodi.
			except: path = xbmc.translatePath(path) # Old Kodi.
			if path: sys.path.append(path)
			try: key = base64.b64decode(base64.b64decode(base64.b64decode(Orionoid.Key))).replace(' ', '') # Python 2
			except: key = str(base64.b64decode(base64.b64decode(base64.b64decode(bytes(Orionoid.Key, 'utf-8')))), 'utf-8').replace(' ', '') # Python 3
			from orion import Orion
			Orionoid.Instance = Orion(key)
		return Orionoid.Instance

	@classmethod
	def streams(self, type, stream, imdb = None, tmdb = None, tvdb = None, trakt = None, season = None, episode = None, count = None):
		try:
			if stream in Orionoid.Instance.streamTypes():
				items = Orionoid.Instance.streams(type = type, streamType = stream, protocolTorrent = Orionoid.Instance.ProtocolMagnet if Orionoid.Instance.StreamTorrent == stream else None, idImdb = imdb, idTmdb = tmdb, idTvdb = tvdb, idTrakt = trakt, numberSeason = season, numberEpisode = episode, details = True)
				if items and 'streams' in items:
					items = items['streams']
					items = [Orionoid.item(item = item, count = count, media_type = type) for item in items]
					items = [item for item in items if item]
					return items
		except: self.error()
		return []

	@classmethod
	def item(self, item, count = None, media_type = None):
		try:
			torrent = item['stream']['type'] == Orionoid.Instance.StreamTorrent
			result = {'package' : 'single', 'info' : []}

			try:
				link = None
				links = item['links']
				for l in links:
					if l.lower().startswith('magnet:'):
						link = l
						break
				result['magnet' if torrent else 'url'] = link if link else links[0]
			except: return None

			provider = Orionoid.Instance.Name
			try: provider += ' - ' + (item['stream']['source'] if torrent or not item['stream']['hoster'] else item['stream']['hoster'])
			except: pass
			result['provider'] = provider
			result['provider_name_override']  = provider
			result['scraper'] = provider

			try: result['hash'] = item['file']['hash']
			except: result['hash'] = ''
			if torrent and not result['hash']: return None # Seren's scraping fails when doing RealDebrid cache lookups and the torrent does not have a hash.

			try: result['release_title'] = item['file']['name'] if item['file']['name'] else ''
			except: result['release_title'] = ''

			try:
				try: pack = item['file']['pack']
				except: pack = False
				result['size'] = (item['file']['size'] / 1048576.0) * (count if count and pack else 1)
			except: result['size'] = 0
			try: result['seeds'] = item['stream']['seeds']
			except: result['seeds'] = 0
			try: result['source'] = item['stream']['source'] if torrent or not item['stream']['hoster'] else item['stream']['hoster']
			except: result['source'] = ''
			try: result['direct'] = item['access']['direct']
			except: result['direct'] = False
			try:
				# Seren does not support movie packs and scraping will fail if Seren tries to estimate the file size of movie packs.
				if media_type in ['show', 'season', 'episode'] and item['file']['pack']: result['package'] = 'season'
			except: pass
			try:
				if item['video']['3d']: result['info'].append('3D')
			except: pass
			try:
				result['info'].append(item['video']['codec'].replace('h2', 'x2'))
				if item['video']['codec'] == 'h265': result['info'].append('HEVC')
				elif item['video']['codec'] == 'h264': result['info'].append('AVC')
			except: pass

			try: audioSystem = item['audio']['system']
			except: audioSystem = None
			try: audioCodec = item['audio']['codec']
			except: audioCodec = None
			if audioSystem == 'dd':
				if audioCodec in ['amsthd', 'amspls', 'ams']: result['info'].append('ATMOS')
				elif audioCodec == 'thd': result['info'].append('TRUEHD')
				elif audioCodec == 'pls': result['info'].append('DD+')
				elif audioSystem == 'dd': result['info'].append('DD')
				elif audioCodec == 'thd': result['info'].append('TRUEHD')
				else:
					result['info'].append('DD')
					result['info'].append(audioCodec)
			elif audioSystem == 'dts':
				if audioCodec == 'hdhra': result['info'].append('DTS-HDHR')
				elif audioCodec == 'hdma': result['info'].append('DTS-HDMA')
				elif audioCodec == 'hd': result['info'].append('DTS-HD')
				elif audioSystem == 'x': result['info'].append('DTS-X')
				else:
					result['info'].append('DTS')
					result['info'].append(audioCodec)
			elif audioCodec:
				result['info'].append(audioCodec)
			elif audioSystem:
				result['info'].append(audioSystem)

			try:
				channels = item['audio']['channels']
				if channels == 2: result['info'].append('2.0')
				elif channels == 6: result['info'].append('5.1')
				elif channels == 8: result['info'].append('7.1')
				elif channels == 10: result['info'].append('9.1')
			except: pass
			try: result['info'].append(item['meta']['edition'])
			except: pass

			try:
				metaRelease = item['meta']['release']
				if metaRelease in ['bdrmx', 'dvdrmx', 'bdmux', 'brmux', 'hddvdmux', 'dvdmux', 'webmux', 'webcapmux', 'webdlmux', 'webhdmux', 'hdtvmux', 'tvmux', 'hdmux', 'mux']: result['info'].append('REMUX')
				elif metaRelease in ['bdrip', 'bdscr', 'bluray', 'bd5', 'bd9', 'bd25', 'bd50', 'bd100', 'bdr', 'brrip']: result['info'].append('BLURAY')
				elif metaRelease in ['dvdrip', 'dvdscr', 'dvd', 'dvdr', 'dvd5', 'dvd9', 'r1', 'r1rip', 'r2', 'r2rip', 'r3', 'r3rip', 'r4', 'r4rip', 'r5', 'r5rip', 'r6', 'r6rip', 'r7', 'r7rip', 'r8', 'r8rip']: result['info'].append('DVDRIP')
				elif metaRelease in ['hdrip']: result['info'].append('HD-RIP')
				elif metaRelease in ['hdtv', 'hdts', 'hdtvrip']: result['info'].append('HDTV')
				elif metaRelease in ['pdtv', 'pdtvrip']: result['info'].append('PDTV')
				elif metaRelease in ['webcap', 'webdl', 'webrip', 'web', 'webcaprip', 'webdlrip', 'webhd', 'webhdrip']: result['info'].append('WEB')
				else: result['info'].append(metaRelease)
			except: pass

			result['quality'] = 'SD'
			try:
				quality = item['video']['quality']
				if quality in [Orionoid.Instance.QualityHd8k, Orionoid.Instance.QualityHd6k, Orionoid.Instance.QualityHd4k, Orionoid.Instance.QualityHd2k]: result['quality'] = '4K'
				elif quality == Orionoid.Instance.QualityHd1080: result['quality'] = '1080p'
				elif quality == Orionoid.Instance.QualityHd720: result['quality'] = '720p'
				elif quality in [Orionoid.Instance.QualityScr1080, Orionoid.Instance.QualityScr720, Orionoid.Instance.QualityScr]: result['info'].append('SCR')
				elif quality in [Orionoid.Instance.QualityCam1080, Orionoid.Instance.QualityCam720, Orionoid.Instance.QualityCam]: result['info'].append('CAM')
			except: pass

			# Convert to string, otherwise Seren reorders the items in the list.
			# Must be added as a list, since Seren later appends items to the list.
			#result['info'] = [' '.join([i for i in result['info'] if i is not None])]
			# Update: New verssion of Seren requires a set instead of a list.
			# Seren specifically looks for upper-case values.
			result['info'] = set([str(i).upper() for i in result['info'] if i is not None])

			result = {k : v for k, v in result.items() if v is not None}
			return result
		except: self.error()
		return None
