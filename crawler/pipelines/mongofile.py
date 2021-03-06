
class MongodbFilesPipeline(FilesPipeline):
    """
        This is for download the book file and then define the book_file_id
        field to the file's gridfs id in the mongodb.
    """

    MEDIA_NAME = 'mongodb_openslackfile'
    EXPIRES = 90
    FILE_CONTENT_TYPE = ['image/png', 'image/jpeg', "image/gif"]
    URL_GBK_DOMAIN = []
    ATTACHMENT_FILENAME_UTF8_DOMAIN = []
    FILES_RESULT_FIELD = "files"
    FILES_URLS_FIELD = "file_urls"
    STORE_SCHEMES = {
        '': MongodbFilesStore,
        'mongodb': MongodbFilesStore,
    }

    FILE_EXTENTION = ['.rar', '.zip', '.pdf', '.tar', '.tar.gz', '.tar.bz2',
                      '.xlsx', 'xls', 'ppt', 'pptx', '.doc', '.txt', '.docx',
                      '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico',
                      ]

    def __init__(self, shard_server, shard_port, shard_db, shard_gridfs_collection, download_func=None):
        self.style = color.color_style()
        ##########from MediaPipeline###########
        self.spiderinfo = {}
        self.download_func = download_func
        ##########from MediaPipeline###########

        self.store = self._get_store(shard_server, shard_port, shard_db, shard_gridfs_collection)
        self.item_download = {}

    @classmethod
    def from_settings(cls, settings):
        cls.EXPIRES = settings.getint('BOOK_FILE_EXPIRES', 90)
        cls.BOOK_FILE_CONTENT_TYPE = settings.get('FILE_CONTENT_TYPE', [])
        cls.ATTACHMENT_FILENAME_UTF8_DOMAIN = settings.get('ATTACHMENT_FILENAME_UTF8_DOMAIN', [])
        cls.URL_GBK_DOMAIN = settings.get('URL_GBK_DOMAIN', [])
        cls.FILE_EXTENTION = settings.get('FILE_EXTENTION', cls.FILE_EXTENTION)
        shard_server = settings.get('ShardMONGODB_SERVER', "localhost")
        shard_port = settings.get('ShardMONGODB_PORT', 27017)
        shard_db = settings.get('ShardMONGODB_DB', "openslack")
        shard_gridfs_collection = settings.get('GridFs_Collection', 'fs')
        return cls(shard_server, shard_port, shard_db, shard_gridfs_collection)

    def _get_store(self, shard_server, shard_port, shard_db, shard_gridfs_collection):
        scheme = 'mongodb'
        store_cls = self.STORE_SCHEMES[scheme]
        return store_cls(shard_server, shard_port, shard_db, shard_gridfs_collection)

    def process_item(self, item, spider):
        """
            custom process_item func,so it will manage the Request result.
        """
        info = self.spiderinfo
        requests = arg_to_iter(self.get_media_requests(item, info))
        dlist = [self._process_request(r, info) for r in requests]
        dfd = DeferredList(dlist, consumeErrors=1)
        return dfd.addCallback(self.item_completed, item, info)

    def another_process_item(self, result, item, info):
        """
            custom process_item func,so it will manage the Request result.
        """

        assert isinstance(result, (Item, Request)), \
            "WoaiduBookFile pipeline' item_completed must return Item or Request, got %s" % \
            (type(result))
        if isinstance(result, Item):
            return result
        elif isinstance(result, Request):
            dlist = [self._process_request(r, info) for r in arg_to_iter(result)]
            dfd = DeferredList(dlist, consumeErrors=1)
            dfd.addCallback(self.item_completed, item, info)
            # XXX:This will cause one item maybe return many times,it depends on how many
            # times the download url failed.But it doesn't matter.Because when raise errors,
            # the items are no longer processed by further pipeline components.And when all
            # url download failed we can drop that item which book_file or book_file_url are
            # empty.
            return dfd.addCallback(self.another_process_item, item, info)
        else:
            raise NofilesDrop

    def get_media_requests(self, item, info):
        """
            Only download once per book,so it pick out one from all of the download urls.
        """
        return [Request(x) for x in item.get(self.FILES_URLS_FIELD, [])]

    def media_downloaded(self, response, request, info):
        """
            Handler for success downloads.
        """
        referer = request.headers.get('Referer')

        if response.status != 200:
            log.msg(
                format='%(medianame)s (code: %(status)s): Error downloading %(medianame)s from %(request)s referred in <%(referer)s>',
                level=log.WARNING, spider=info.spider, medianame=self.MEDIA_NAME,
                status=response.status, request=request, referer=referer)
            raise FileException(request.url, '%s: download-error' % (request.url,))

        if not response.body:
            log.msg(
                format='%(medianame)s (empty-content): Empty %(medianame)s from %(request)s referred in <%(referer)s>: no-content',
                level=log.WARNING, spider=info.spider, medianame=self.MEDIA_NAME,
                request=request, referer=referer)
            raise FileException(request.url, '%s: empty-content' % (request.url,))

        status = 'cached' if 'cached' in response.flags else 'downloaded'
        log.msg(
            format='%(medianame)s (%(status)s): Downloaded %(medianame)s from %(request)s referred in <%(referer)s>',
            level=log.DEBUG, spider=info.spider, medianame=self.MEDIA_NAME,
            status=status, request=request, referer=referer)

        if self.is_valid_content_type(response):
            raise FileException(request.url, '%s: invalid-content_type' % (request.url,))

        filename = self.get_file_name(request, response)

        if not filename:
            raise FileException(request.url, '%s: noaccess-filename' % (request.url,))

        self.inc_stats(info.spider, status)

        try:
            key = self.file_key(info.spider.name, request.url)  # return the SHA1 hash of the file url
            book_file_id, checksum = self.store.persist_file(key, response.body, info, filename, url=request.url)
        except FileException as exc:
            whyfmt = '%(medianame)s (error): Error processing %(medianame)s from %(request)s referred in <%(referer)s>: %(errormsg)s'
            log.msg(format=whyfmt, level=log.WARNING, spider=info.spider, medianame=self.MEDIA_NAME,
                    request=request, referer=referer, errormsg=str(exc))
            raise

        # print {'url': request.url, 'file_id': book_file_id, 'checksum': checksum}
        return {'url': request.url, 'file_id': book_file_id, 'checksum': checksum}

    def media_to_download(self, request, info):
        def _onsuccess(result):

            if not result:
                return  # returning None force download

            last_modified = result.get('last_modified', None)
            if not last_modified:
                return  # returning None force download

            timedelta_obj = datetime.datetime.now() - last_modified
            age_seconds = timedelta_obj.total_seconds()
            age_days = age_seconds / 60 / 60 / 24
            if age_days > self.EXPIRES:
                return  # returning None force download

            referer = request.headers.get('Referer')
            log.msg(
                format='%(medianame)s (uptodate): Downloaded %(medianame)s from %(request)s referred in <%(referer)s>',
                level=log.DEBUG, spider=info.spider,
                medianame=self.MEDIA_NAME, request=request, referer=referer)
            self.inc_stats(info.spider, 'uptodate')

            checksum = result.get('checksum', None)
            # print request.url,"----------------------"
            return {'url': request.url, 'file_id': key, 'checksum': checksum}

        key = self.file_key(info.spider.name, request.url)  # return the SHA1 hash of the file url
        dfd = defer.maybeDeferred(self.store.stat_file, key, info)
        dfd.addCallbacks(_onsuccess, lambda _: None)
        dfd.addErrback(log.err, self.__class__.__name__ + '.store.stat_file')
        return dfd

    def file_key(self, spider_name, url):
        """
            return the SHA1 hash of the file url
        """
        file_guid = hashlib.sha1(url).hexdigest()
        # return '%s_%s' % (urlparse(url).netloc,file_guid)
        return '%s_%s' % (spider_name, file_guid)

    def item_completed(self, results, item, info):
        if self.LOG_FAILED_RESULTS:
            for ok, value in results:
                if not ok:
                    log.error(
                        '%(class)s found errors processing %(item)s',
                        {'class': self.__class__.__name__, 'item': item},
                        exc_info=failure_to_exc_info(value),
                        extra={'spider': info.spider}
                    )
        item["files"] = [{"file_id": x['file_id'], "url": x['url']} for ok, x in results if ok]
        return item

    def is_valid_content_type(self, response):
        """
            judge whether is it a valid response by the Content-Type.
        """
        content_type = response.headers.get('Content-Type', '')
        # return content_type not in self.FILE_CONTENT_TYPE
        return False

    def get_file_name(self, request, response):
        """
            Get the raw file name that the sever transfer to.

            It examine two places:Content-Disposition,url.
        """
        content_dispo = response.headers.get('Content-Disposition', '')
        filename = ""
        if content_dispo:
            for i in content_dispo.split(';'):
                if "filename" in i:
                    # XXX:use filename= for the specific case that = in the filename
                    filename = i.split('filename=')[1].strip(" \n\'\"")
                    break

        if filename:
            # XXX:it the result is:
            # MPLS TE Switching%E6%96%B9%E6%A1%88%E7%99%BD%E7%9A%AE%E4%B9%A6.pdf
            # use urllib.unquote(filename) instead
            if urlparse(request.url).netloc in self.ATTACHMENT_FILENAME_UTF8_DOMAIN:
                filename = filename.decode("utf-8")
            else:
                filename = filename.decode("gbk")
                # print "Content-Disposition:","*"*30,filename
        else:
            guessname = request.url.split('?')[0].split("/")[-1]
            # os.path.splitext:
            # Split the pathname path into a pair (root, ext) such that root + ext == path
            if os.path.splitext(guessname)[1].lower() in self.FILE_EXTENTION:
                if urlparse(request.url).netloc in self.URL_GBK_DOMAIN:
                    filename = urllib.unquote(guessname).decode("gbk").encode("utf-8")
                else:
                    filename = urllib.unquote(guessname)
                    # print "url:","*"*30,filename
            else:
                filename = guessname
            if filename.count(".") == 0:
                filename += ".jpg"
        return filename


class MongodbFilesStore(FSFilesStore):
    """
        save book file to gridfs of mongodb.
    """

    ShardMONGODB_SERVER = "localhost"
    ShardMONGODB_PORT = 27017
    ShardMONGODB_DB = "openslack"
    GridFs_Collection = "fs"

    def __init__(self, shard_server, shard_port, shard_db, shard_gridfs_collection):
        self.style = color.color_style()
        self.shard_gridfs_collection = shard_gridfs_collection
        try:
            self.client = MongoClient(shard_server, shard_port)
            # self.inti_fs(shard_db)
        except Exception as e:
            print self.style.ERROR("ERROR(MongodbFilesStore): %s" % (str(e),))
            traceback.print_exc()

    def inti_fs(self, key):
        self.db = self.client[key]
        self.fs = gridfs.GridFS(self.db, self.shard_gridfs_collection)

    def persist_file(self, key, file_content, info, filename, url=None):
        self.inti_fs(key.split("_")[0])
        contentType = os.path.splitext(filename)[1][1:].lower()
        book_file_id = self.fs.put(file_content, _id=key, filename=filename, contentType=contentType, url=url)
        checksum = self.fs.get(book_file_id).md5
        return (book_file_id, checksum)

    def stat_file(self, key, info):
        """
            the stat is the file key dir,
            the last_modified is the file that saved to the file key dir.
        """
        self.inti_fs(key.split("_")[0])
        checksum = self.fs.get(key).md5
        last_modified = self.fs.get(key).upload_date

        return {'last_modified': last_modified, 'checksum': checksum}
