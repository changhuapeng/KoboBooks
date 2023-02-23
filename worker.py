#!/usr/bin/env python
# vim:fileencoding=UTF-8:ts=4:sw=4:sta:et:sts=4:ai
from __future__ import absolute_import, division, print_function, unicode_literals

__license__ = "GPL v3"
__copyright__ = "2014, David Forrester <davidfor@internode.on.net>"
__docformat__ = "restructuredtext en"

import datetime
import re
import socket
from threading import Thread

from calibre.ebooks.metadata.book.base import Metadata
from calibre.library.comments import sanitize_comments_html
from calibre.utils.cleantext import clean_ascii_chars
from calibre.utils.icu import lower
from calibre_plugins.kobobooks import KoboBooks
from lxml.html import fromstring, tostring


class Worker(Thread):  # Get details

    """
    Get book details from Kobo Books book page in a separate thread
    """

    def __init__(
        self,
        url,
        publisher,
        match_authors,
        result_queue,
        browser,
        log,
        relevance,
        category_handling,
        plugin,
        timeout=20,
    ):
        Thread.__init__(self)
        self.daemon = True
        self.url, self.result_queue = url, result_queue
        self.publisher, self.match_authors = publisher, match_authors
        self.log, self.timeout = log, timeout
        self.relevance, self.plugin = relevance, plugin
        self.browser = browser.clone_browser()
        self.cover_url = self.kobobooks_id = self.isbn = None
        self.category_handling = category_handling

    def run(self):
        try:
            self.get_details()
        except:
            self.log.exception("get_details failed for url: %r" % self.url)

    def get_details(self):
        try:
            self.log.info("KoboBooks url: %r" % self.url)
            raw = (
                self.browser.open_novisit(self.url, timeout=self.timeout).read().strip()
            )
        except Exception as e:
            if callable(getattr(e, "getcode", None)) and e.getcode() == 404:
                self.log.error("URL malformed: %r" % self.url)
                return
            attr = getattr(e, "args", [None])
            attr = attr if attr else [None]
            if isinstance(attr[0], socket.timeout):
                msg = "Kobo Books timed out. Try again later."
                self.log.error(msg)
            else:
                msg = "Failed to make details query: %r" % self.url
                self.log.exception(msg)
            return

        raw = raw.decode("utf-8", errors="replace")
        # open('E:\\t3.html', 'wb').write(raw)

        if "<title>404 - " in raw:
            self.log.error("URL malformed: %r" % self.url)
            return

        try:
            root = fromstring(clean_ascii_chars(raw))
        except:
            msg = "Failed to parse Kobo Books details page: %r" % self.url
            self.log.exception(msg)
            return

        self.parse_details(root)

    def parse_details(self, root):
        self.log()
        try:
            kobobooks_id = self.parse_kobobooks_id(self.url)
            self.log('parse_details - kobobooks_id: "%s" ' % (kobobooks_id))
        except:
            self.log.exception("Error parsing URL for Kobo Books: %r" % self.url)
            kobobooks_id = None

        try:
            title = self.parse_title(root)
            self.log('parse_details - title: "%s" ' % (title))
        except:
            self.log.exception("Error parsing page for title: url=%r" % self.url)
            title = None

        try:
            # self.log('parse_details - root: ',tostring(root))
            authors = self.parse_authors(root)
            self.log('parse_details - authors: "%s" ' % (authors))
        except:
            self.log.exception("Error parsing page for authors: url=%r" % self.url)
            authors = []

        if not title or not authors or not kobobooks_id:
            self.log.error(
                "Could not find title/authors/KoboBooks id for %r" % self.url
            )
            self.log.error(
                "Kobo Books: %r Title: %r Authors: %r" % (kobobooks_id, title, authors)
            )
            return

        mi = Metadata(title, authors)
        mi.set_identifier("kobo", kobobooks_id)
        self.kobobooks_id = kobobooks_id

        # Some of the metadata is in a JSON object in div[@data-kobo-gizmo-config] tag.
        try:
            import json

            results = root.xpath(
                '//div[@data-kobo-gizmo="RatingAndReviewWidget"]/@data-kobo-gizmo-config'
            )

            if len(results) > 0:
                json_details = results[0]
                if json_details is not None:
                    page_json = json.loads(json_details, strict=False)

                    page_details = page_json["googleProduct"]
                    page_metadata = json.loads(page_details, strict=False)
                    # self.log("Result page_metadata=", page_metadata)
                    # self.log("Script page_metadata keys=", page_metadata.keys())
                    try:
                        pubdate = page_metadata["releasedate"]
                        pubdate = datetime.datetime.strptime(
                            pubdate, "%Y-%m-%dT%H:%M:%S"
                        )
                        mi.pubdate = pubdate
                        self.log("pubdate from JSON:", mi.pubdate)
                    except:
                        self.log.exception(
                            "Error parsing page for pubdate: url=%r" % self.url
                        )

                    try:
                        mi.publisher = page_metadata["brand"]["name"]
                    except:
                        self.log.exception(
                            "Error parsing page for publisher: url=%r" % self.url
                        )

                    try:
                        isbn = page_metadata["gtin13"]
                        if isbn:
                            self.isbn = mi.isbn = isbn
                    except:
                        self.log.exception(
                            "Error parsing page for ISBN: url=%r" % self.url
                        )

            else:
                self.log("No JSON found for book details metadata????")
        except Exception as e:
            self.log("Exception thrown getting JSON:", e)

        try:
            (mi.series, mi.series_index) = self.parse_series(root)
        except:
            self.log.exception("Error parsing series for url: %r" % self.url)

        try:
            mi.tags = self.parse_tags(root)
        except:
            self.log.exception("Error parsing tags for url: %r" % self.url)

        try:
            mi.rating = self.parse_rating(root)
        except:
            self.log.exception("Error parsing ratings for url: %r" % self.url)

        try:
            self.cover_url = self.parse_cover(root)
        except:
            self.log.exception("Error parsing cover for url: %r" % self.url)
        mi.has_cover = bool(self.cover_url)

        try:
            mi.comments = self.parse_comments(root)
        except:
            self.log.exception("Error parsing comments for url: %r" % self.url)

        try:
            language = self.parse_language(root)
            if language:
                self.lang = mi.language = language
        except:
            self.log.exception("Error parsing languages for url: %r" % self.url)

        mi.source_relevance = self.relevance

        if self.kobobooks_id:
            if self.cover_url:
                self.plugin.cache_identifier_to_cover_url(
                    self.kobobooks_id, self.cover_url
                )

        self.plugin.clean_downloaded_metadata(mi)

        self.result_queue.put(mi)

    def parse_kobobooks_id(self, url):
        return re.search(
            KoboBooks.STORE_DOMAIN + ".*" + KoboBooks.BOOK_PATH + "(.*)", url
        ).groups(0)[0]
        # return re.search('store.kobobooks.com/en-US/ebook/(.*)', url).groups(0)[0]

    def parse_title(self, root):
        title_node = root.xpath('//h1[@class="title product-field"]')
        if title_node:
            return title_node[0].text.strip()

    def parse_series(self, root):
        series_name = None
        series_index = None
        series_name_node = root.xpath(
            '//span[@class="series product-field"]/span[@class="product-sequence-field"]/a'
        )
        series_index_node = root.xpath(
            '//span[@class="series product-field"]/span[@class="sequenced-name-prefix"]'
        )

        if series_name_node and len(series_name_node) > 0:
            series_name = series_name_node[0].text.strip()
            self.log('parse_series - series_name: "%s" ' % series_name)
            self.log(
                'parse_series - series_name_node: "%s" '
                % (tostring(series_name_node[0]))
            )

            if series_index_node and len(series_index_node) > 0:
                series_index_node = series_index_node[0]
                self.log(
                    'parse_series - series_index_node: "%s" '
                    % (tostring(series_index_node))
                )
                self.log(
                    'parse_series - series_index_node.text: "%s" '
                    % (series_index_node.text)
                )
                series_index_text = series_index_node.text
                self.log(
                    'parse_series - series_index_text: "%s" ' % (series_index_text)
                )
                try:
                    series_index = series_index_text.split(" ")[1]
                    self.log(
                        'parse_series - series_index after split: "%s" '
                        % (series_index)
                    )
                    series_index = int(series_index)
                except:
                    self.log(
                        "parse_series - Unable to get series number from series info"
                    )

        self.log(
            "parse_series - series_name=%s, series_index=%s"
            % (series_name, series_index)
        )
        return (series_name, series_index)

    def parse_authors(self, root):
        # self.log('parse_authors - root: "%s"' % root)
        author = ",".join(root.xpath('//a[@class="contributor-name"]/text()'))
        author = author.strip()
        self.log('parse_authors - author: "%s"' % author)
        # author = author.split('by ')[-1]
        authors = [a.strip() for a in author.split(",")]
        self.log('parse_authors - authors: "%s"' % authors)

        def ismatch(authors):
            authors = lower(" ".join(authors))
            amatch = not self.match_authors
            for a in self.match_authors:
                if lower(a) in authors:
                    amatch = True
                    break
            if not self.match_authors:
                amatch = True
            return amatch

        if author == "" or not self.match_authors or ismatch(authors):
            # self.log('parse_authors - authors:', authors)
            return authors
        self.log("Rejecting authors as not a close match: ", ",".join(authors))

    def parse_comments(self, root):
        # The comments seem to have two slightly different containing divs.
        description_node = root.xpath('//div[@class="synopsis-description-all"]')
        # self.log('parse_comments - description_node: "%s" ' % (description_node))
        # self.log('parse_comments - len(description_node): "%s" ' % (len(description_node)))
        # if len(description_node):
        #     self.log('parse_comments - tostring(description_node[0]): "%s" ' % (tostring(description_node[0])))
        try:
            description_node = description_node[0]
        except:
            description_node = root.xpath('//div[@class="synopsis-description"]')
            try:
                description_node = description_node[0]
            except:
                description_node = None

        if description_node is not None:
            comments = tostring(description_node, method="html")
            # comments = sanitize_comments_html(comments)
            return comments
        self.log("parse_comments - no comments found.")

    def parse_rating(self, root):
        rating_node = root.xpath('//meta[@property="og:rating"]/@content')
        if rating_node:
            try:
                rating_text = rating_node[0]
                rating_value = float(rating_text)
                self.log('parse_rating - rating: "%s"' % rating_text)
                return rating_value
            except:
                self.log('parse_rating - rating: "%s"' % "None")
                return None

    def parse_cover(self, root):
        # Kobo have a higher resolution covers that get downloaded directly to the device. The images
        # can be retrieved by setting the size of the cover. Kobo will send a cover that fits in this size
        # and respects the aspect ratio of the stored cover.
        # For example, the cover node is:
        #    //kbimages1-a.akamaihd.net/019f9050-d9a5-4a4f-9720-e4abcdea627b/353/569/90/False/turn-coat.jpg
        # In this, the image is 353x569.
        # For the size, use the "maximum_cover_size" tweak. if this is not set, use 1650x2200. This is the detault for
        # the tweak at the time writing.
        from calibre.utils.config_base import tweaks

        nwidth, nheight = tweaks["maximum_cover_size"]
        cover_node = root.xpath(
            '//div[@class="main-product-image"]//img[contains(@class,"cover-image")]/@src'
        )[0]
        cover_node_text = "%s %s" % ("http:", cover_node)
        cover_node_split = cover_node_text.split("/")
        cover_width_upsized = int(cover_node_split[4]) * 4
        cover_height_upsized = int(cover_node_split[5]) * 4
        cover_node_split[4] = str(nwidth)
        cover_node_split[5] = str(nheight)
        cover_node = "/".join(cover_node_split).replace(" ", "")

        # if cover_node:
        #     match = re.match('popupimg\(\'(.*)\'\)', cover_node[0])
        #     if match:
        #         return KoboBooks.BASE_URL + KoboBooks.BOOK_PATH + match.groups(0)[0]
        #         return 'http://store.kobobooks.com/en-US/ebook/' + match.groups(0)[0]

        return cover_node

    def parse_language(self, root):
        lang_node = root.xpath(
            '//div[@class="bookitem-secondary-metadata"]/ul/li[contains(text(), "Language:")]'
        )[0]
        if lang_node.text.strip() == "Language:":
            language = lang_node.xpath("./span")[0].text
            self.log('parse_language - language: "%s" ' % (language))
            return language

    def parse_tags(self, root):
        ans = []
        # There are no exclusions at this point.
        exclude_tokens = {}
        exclude = {}
        seen = set()
        category_node = root.xpath('//ul[@class="category-rankings"]')
        # self.log('parse_tags - category_node: "%s" ' % (category_node))
        # self.log('parse_tags - len(category_node): "%s" ' % (len(category_node)))
        # self.log('parse_tags - tostring(category_node[0]): "%s" ' % (tostring(category_node[0])))
        if len(category_node) > 0:
            for li in category_node[0].xpath("./li"):
                # self.log('parse_tags - li: "%s" ' % (li))
                # self.log('parse_tags - len(li): "%s" ' % (len(li)))
                # self.log('parse_tags - tostring(li[0]): "%s" ' % (tostring(li[0])))
                tag = ""
                for i, a in enumerate(li.iterdescendants("a")):
                    # self.log('parse_tags - a: "%s" ' % (a))
                    # self.log('parse_tags - len(a): "%s" ' % (len(a)))
                    # if len(a) > 0:
                    #     self.log('parse_tags - tostring(a[0]): "%s" ' % (tostring(a[0])))
                    if self.category_handling == "top_level_only" and i > 0:
                        self.log("parse_tags - top level only and sub level category")
                        continue
                    raw = (a.text or "").strip().replace(",", ";")
                    if self.category_handling == "hierarchy" and i > 0:
                        tag = tag + "." + raw
                    else:
                        tag = raw
                    ltag = icu_lower(tag)
                    tokens = frozenset(ltag.split())
                    if (
                        tag
                        and ltag not in exclude
                        and not tokens.intersection(exclude_tokens)
                        and ltag not in seen
                    ):
                        ans.append(tag)
                        seen.add(ltag)
        return ans
