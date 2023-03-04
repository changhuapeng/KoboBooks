#!/usr/bin/env python
# vim:fileencoding=UTF-8:ts=4:sw=4:sta:et:sts=4:ai
from __future__ import absolute_import, division, print_function, unicode_literals

__license__ = "GPL v3"
__copyright__ = "2015, David Forrester <davidfor@internode.on.net>"
__docformat__ = "restructuredtext en"

import re
import time

try:
    from urllib.parse import quote, unquote
except ImportError:
    from urllib import quote, unquote
try:
    from queue import Empty, Queue
except ImportError:
    from Queue import Empty, Queue

import six
from calibre import as_unicode
from calibre.ebooks.metadata import check_isbn
from calibre.ebooks.metadata.sources.base import Option, Source
from calibre.utils.cleantext import clean_ascii_chars
from calibre.utils.icu import lower
from calibre.utils.localization import get_udc
from lxml.html import fromstring, tostring
from six import text_type as unicode


class KoboBooks(Source):
    name = "Kobo Books"
    description = "Downloads metadata and covers from kobobooks.com"
    author = "David Forrester"
    version = (1, 10, 1)
    minimum_calibre_version = (0, 8, 0)

    ID_NAME = "kobo"
    capabilities = frozenset(["identify", "cover"])
    touched_fields = frozenset(
        [
            "title",
            "authors",
            "identifier:kobo",
            "rating",
            "languages",
            "comments",
            "publisher",
            "pubdate",
            "series",
            "tags",
        ]
    )
    has_html_comments = True
    supports_gzip_transfer_encoding = True

    # STORE_DOMAIN = "store.kobobooks.com"
    STORE_DOMAIN = "www.kobo.com"
    BASE_URL = "https://" + STORE_DOMAIN
    BOOK_PATH = "/ebook/"
    # BOOK_PATH = '/au/sv/ebook/'
    SEARCH_PATH = "/search"
    # SEARCH_PATH = '/au/fr/search'
    # SEARCH_PATH = '/us/en/search'
    # SEARCH_PATH = '/fr/en/search'
    # SEARCH_PATH = '/au/sv/search'

    CATEGORY_HANDLING = {
        "top_level_only": "Top level only",
        "hierarchy": "Hierarchy",
        "individual_tags": "Individual tags",
    }

    options = (
        Option(
            "category_handling",
            "choices",
            "individual_tags",
            "Category handling:",
            "How to handle categories if they have more than one level.",
            choices=CATEGORY_HANDLING,
        ),
    )

    @property
    def category_handling(self):
        x = getattr(self, "cat_handling", None)
        if x is not None:
            return x
        cat_handling = self.prefs["category_handling"]
        if cat_handling not in self.CATEGORY_HANDLING:
            cat_handling = self.CATEGORY_HANDLING[0]

        return cat_handling

    @property
    def user_agent(self):
        # Use the user agent used by mechanize to get around the bot protection.
        return "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0.3 Safari/605.1.15"
        # return 'Python-urllib/3.9'

    def get_book_url(self, identifiers):
        kobobooks_id = identifiers.get("kobo", None)
        if kobobooks_id:
            return (
                self.ID_NAME,
                kobobooks_id,
                "%s%s%s" % (KoboBooks.BASE_URL, KoboBooks.BOOK_PATH, kobobooks_id),
            )

    def id_from_url(self, url):
        # print("KoboBooks::id_from_url - url=", url)
        # print("KoboBooks::id_from_url - generic pattern:", self.BASE_URL + ".*" + self.BOOK_PATH + "(.*)?", url)
        match = re.match(self.BASE_URL + ".*" + self.BOOK_PATH + "(.*)?", url)
        if match:
            # print("KoboBooks::id_from_url - have match using generic URL")
            return (self.ID_NAME, match.groups(0)[0])
        # https://www.kobo.com/au/en/ebook/the-rogue-prince-4
        # print("KoboBooks::id_from_url - National pattern:", self.BASE_URL + "\/\w\w\/\w\w" + self.BOOK_PATH + "(.*)?")
        match = re.match(self.BASE_URL + "/\w\w/\w\w" + self.BOOK_PATH + "(.*)?", url)
        if match:
            # print("KoboBooks::id_from_url - have match using national URL")
            return (self.ID_NAME, match.groups(0)[0])
        return None

    def get_browser(self):
        # Add some extra header attributes to the browser.
        # Note: These have been determined via trial-and-error.
        br = self.browser
        br.set_current_header(
            "Accept",
            "text/html,application/xhtml+xml,application/xml;q=0.9,image/*,*/*;q=0.8",
        )
        br.set_current_header("Accept-Language", "en-US,en-UK,en;q=0.9,ja;q=0.8")

        return br

    def get_cached_cover_url(self, identifiers):
        url = None
        kobobooks_id = identifiers.get(self.ID_NAME, None)
        if kobobooks_id is None:
            isbn = identifiers.get("isbn", None)
            if isbn is not None:
                kobobooks_id = self.cached_isbn_to_identifier(isbn)
        if kobobooks_id is not None:
            url = self.cached_identifier_to_cover_url(kobobooks_id)
        return url

    def create_query(self, log, title=None, authors=None, identifiers={}):
        q = ""
        isbn = check_isbn(identifiers.get("isbn", None))
        if isbn is not None:
            q = "query=%s&fcmedia=Book" % isbn
        elif title:
            log('create_query - title: "%s"' % (title))
            title = get_udc().decode(title)
            log('create_query - after decode title: "%s"' % (title))
            tokens = []
            title_tokens = list(
                self.get_title_tokens(title, strip_joiners=False, strip_subtitle=True)
            )
            log('create_query - title_tokens: "%s"' % (title_tokens))
            author_tokens = self.get_author_tokens(authors, only_first_author=True)
            tokens += title_tokens
            tokens += author_tokens
            tokens = [
                quote(t.encode("utf-8") if isinstance(t, unicode) else t)
                for t in tokens
            ]
            q = "+".join(tokens)
            q = "query=%s&fcmedia=Book" % q
        if not q:
            return None

        return "%s%s?%s&fclanguages=all" % (
            KoboBooks.BASE_URL,
            KoboBooks.SEARCH_PATH,
            q,
        )

    def identify(
        self,
        log,
        result_queue,
        abort,
        title=None,
        authors=None,
        identifiers={},
        timeout=30,
    ):
        """
        Note this method will retry without identifiers automatically if no
        match is found with identifiers.
        """
        matches = []
        log('identify - title: "%s" authors= "%s"' % (title, authors))

        # If we have a KoboBooks id then we do not need to fire a "search".
        # Instead we will go straight to the URL for that book.
        kobobooks_id = identifiers.get(self.ID_NAME, None)
        br = self.get_browser()

        if kobobooks_id:
            matches.append(
                (
                    "%s%s%s" % (KoboBooks.BASE_URL, KoboBooks.BOOK_PATH, kobobooks_id),
                    None,
                )
            )
            # log("identify - kobobooks_id=", kobobooks_id)
            # log("identify - matches[0]=", matches[0])
        else:
            query = self.create_query(
                log, title=title, authors=authors, identifiers=identifiers
            )
            if query is None:
                log.error("Insufficient metadata to construct query")
                return
            try:
                log.info("Querying: %s" % query)
                response = br.open_novisit(query, timeout=timeout)
                raw = response.read()
                # raw = br.open(query, timeout=timeout).read()
                # open('E:\\t.html', 'wb').write(raw)
            except Exception as e:
                err = "Failed to make identify query: %r" % query
                log.exception(err)
                return as_unicode(e)
            root = fromstring(clean_ascii_chars(raw))

            # If we have an ISBN then we have to check if Kobo redirected
            # us from the search result straight to the URL for that book.
            isbn = check_isbn(identifiers.get("isbn", None))
            if isbn is not None:
                query_path = query.replace(
                    "%s%s" % (KoboBooks.BASE_URL, KoboBooks.SEARCH_PATH), ""
                )
                query_result = response.geturl()
                if query_path not in query_result:
                    matches.append(
                        (
                            query_result,
                            None,
                        )
                    )
            else:
                # Now grab the match from the search result, provided the
                # title appears to be for the same book
                self._parse_search_results(log, title, root, matches, timeout)

        if abort.is_set():
            return

        if not matches:
            if identifiers and title and authors:
                log(
                    "No matches found with identifiers, retrying using only"
                    " title and authors. Query: %r" % query
                )
                return self.identify(
                    log,
                    result_queue,
                    abort,
                    title=title,
                    authors=authors,
                    timeout=timeout,
                )
            log.error("No matches found with query: %r" % query)
            return

        from calibre_plugins.kobobooks.worker import Worker

        author_tokens = list(self.get_author_tokens(authors))
        workers = [
            Worker(
                data[0],
                data[1],
                author_tokens,
                result_queue,
                br,
                log,
                i,
                self.category_handling,
                self,
            )
            for i, data in enumerate(matches)
        ]

        for w in workers:
            w.start()
            # Don't send all requests at the same time
            time.sleep(0.1)

        while not abort.is_set():
            a_worker_is_alive = False
            for w in workers:
                w.join(0.2)
                if abort.is_set():
                    break
                if w.is_alive():
                    a_worker_is_alive = True
            if not a_worker_is_alive:
                break

        return None

    def _parse_search_results(self, log, orig_title, root, matches, timeout):
        def ismatch(title):
            title = lower(title)
            match = not title_tokens
            for t in title_tokens:
                if lower(t) in title:
                    match = True
                    break
            return match

        title_tokens = list(self.get_title_tokens(orig_title))
        max_results = 5
        for data in root.xpath('//div[@class="SearchResultsWidget"]/section/div/ul/li'):
            # log.error('data: %s' % (tostring(data)))
            try:  # Seem to be getting two different search results pages. Try the most common first.
                item_info = data.xpath('./div/div/div[@class="item-info"]')[0]
                log.error("used three divs item_info")
            except:
                log.error("failed using three divs")
                item_info = data.xpath('./div/div[@class="item-info"]')[0]

            # log.error('item_info: ', item_info)
            # log.error("item_info.xpath('./p/a/@href'): %s" % (item_info.xpath('./p/a/@href')))
            # log.error("item_info.xpath('./p/a/@href')[0]: %s" % (tostring(item_info.xpath('./p/a/@href')[0])))
            title_ref = item_info.xpath("./h2/a")[0]
            # log.error("title_ref: ", tostring(title_ref))
            kobobooks_id = title_ref.xpath("./@href")[0]
            # log.error("kobobooks_id: ", kobobooks_id)
            kobobooks_id = kobobooks_id.split("/")
            # log.error("kobobooks_id: ", kobobooks_id)
            kobobooks_id = kobobooks_id[-1].strip()
            # log.error("kobobooks_id: '%s'" % (kobobooks_id))
            # kobobooks_id = kobobooks_id[len(kobobooks_id) - 1]
            # log.error("kobobooks_id: ", kobobooks_id)
            # log('_parse_search_results - kobobooks_id: %s'%(kobobooks_id))
            if not id:
                continue

            # log.error('data: %s'%(tostring(data.xpath('./a'))))
            # log.error('data: %s'%(tostring(data.xpath('./a/p'))))
            # log.error('data: %s'%(data.xpath('./a/p/span/text()')))
            title = title_ref.text
            # log.error("title: '%s'" % (title))
            if not ismatch(title):
                log.error("Rejecting as not close enough match: %s" % (title))
                continue
            log.error(
                "Have close enough match - title='%s', id='%s'" % (title, kobobooks_id)
            )
            publisher = ""  # .join(data.xpath('./li/a/a/text()'))
            url = "%s%s%s" % (KoboBooks.BASE_URL, KoboBooks.BOOK_PATH, kobobooks_id)
            matches.append((url, publisher))
            if len(matches) >= max_results:
                break

    def download_cover(
        self,
        log,
        result_queue,
        abort,
        title=None,
        authors=None,
        identifiers={},
        timeout=30,
    ):
        cached_url = self.get_cached_cover_url(identifiers)
        if cached_url is None:
            log.info("No cached cover found, running identify")
            rq = Queue()
            self.identify(
                log, rq, abort, title=title, authors=authors, identifiers=identifiers
            )
            if abort.is_set():
                return
            results = []
            while True:
                try:
                    results.append(rq.get_nowait())
                except Empty:
                    break
            results.sort(
                key=self.identify_results_keygen(
                    title=title, authors=authors, identifiers=identifiers
                )
            )
            for mi in results:
                cached_url = self.get_cached_cover_url(mi.identifiers)
                if cached_url is not None:
                    break
        if cached_url is None:
            log.info("No cover found")
            return

        if abort.is_set():
            return
        br = self.get_browser()
        log("Downloading cover from:", cached_url)
        try:
            cdata = br.open_novisit(cached_url, timeout=timeout).read()
            result_queue.put((self, cdata))
        except:
            log.exception("Failed to download cover from:", cached_url)


if __name__ == "__main__":  # tests
    # To run these test use:
    # calibre-debug -e __init__.py
    from calibre.ebooks.metadata.sources.test import (
        authors_test,
        series_test,
        test_identify_plugin,
        title_test,
    )

    test_identify_plugin(
        KoboBooks.name,
        [
            (  # A book with no ISBN specified
                {"title": "Turn Coat", "authors": ["Jim Butcher"]},
                [
                    title_test("Turn Coat", exact=True),
                    authors_test(["Jim Butcher"]),
                    series_test("Dresden Files", 11.0),
                ],
            ),
            (  # A book with an ISBN
                {
                    "identifiers": {"isbn": "9780748111824"},
                    "title": "Turn Coat",
                    "authors": ["Jim Butcher"],
                },
                [
                    title_test("Turn Coat", exact=True),
                    authors_test(["Jim Butcher"]),
                    series_test("Dresden Files", 11.0),
                ],
            ),
            (  # A book with a KoboBooks id
                {
                    "identifiers": {"kobo": "across-the-sea-of-suns-1"},
                    "title": "Across the Sea of Suns",
                    "authors": ["Gregory Benford"],
                },
                [
                    title_test("Across the Sea of Suns", exact=True),
                    authors_test(["Gregory Benford"]),
                    series_test("Galactic Centre", 2.0),
                ],
            ),
        ],
    )
