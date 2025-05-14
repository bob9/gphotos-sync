import logging
import os.path
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from gphotos_sync.Checks import get_check

from . import Utils
from .GoogleAlbumMedia import GoogleAlbumMedia
from .GoogleAlbumsRow import GoogleAlbumsRow
from .GooglePhotosMedia import GooglePhotosMedia
from .GooglePhotosRow import GooglePhotosRow
from .LocalData import LocalData
from .restclient import RestClient
from .Settings import Settings

log = logging.getLogger(__name__)

PAGE_SIZE = 100
ALBUM_ITEMS = 50


class GoogleAlbumsSync(object):
    """A Class for managing the indexing and download Google of Albums"""

    def __init__(
        self,
        api: RestClient,
        root_folder: Path,
        db: LocalData,
        flush: bool,
        settings: Settings,
    ):
        """
        Parameters:
            root_folder: path to the root of local file synchronization
            api: object representing the Google REST API
            db: local database for indexing
            settings: further arguments
        """
        self._photos_folder = settings.photos_path
        self._albums_folder = settings.albums_path
        self._shared_albums_folder = settings.shared_albums_path

        self._root_folder: Path = root_folder
        if not self._albums_folder.is_absolute():
            self._albums_root = self._root_folder / self._albums_folder
        else:
            self._albums_root = self._albums_folder
        if not self._shared_albums_folder.is_absolute():
            self._shared_albums_root = self._root_folder / self._shared_albums_folder
        else:
            self._shared_albums_root = self._shared_albums_folder
        self._photos_root = self._root_folder / self._photos_folder
        self._db: LocalData = db
        self._api: RestClient = api
        self.flush = flush

        self.settings = settings
        self.album = settings.album
        self.album_regex = settings.album_regex
        self.shared_albums = settings.shared_albums
        self.album_index = settings.album_index
        self.use_start_date = settings.use_start_date
        self.favourites = settings.favourites_only
        self.include_video = settings.include_video
        self._use_flat_path = settings.use_flat_path
        self._omit_album_date = settings.omit_album_date
        self._album_invert = settings.album_invert
        self._use_hardlinks = settings.use_hardlinks
        self._ntfs_override = settings.ntfs_override
        self.month_format = settings.month_format
        self.path_format = settings.path_format
        self._no_album_sorting = settings.no_album_sorting
        self._preserve_album_links = settings.preserve_album_links

    @classmethod
    def make_search_parameters(
        cls, album_id: str, page_token: Optional[str] = None
    ) -> Dict:
        body = {"pageToken": page_token, "albumId": album_id, "pageSize": PAGE_SIZE}
        return body

    def fetch_album_contents(
        self, album_id: str, add_media_items: bool
    ) -> Tuple[datetime, datetime]:
        first_date = Utils.maximum_date()
        last_date = Utils.MINIMUM_DATE
        body = self.make_search_parameters(album_id=album_id)
        response = self._api.mediaItems.search.execute(body)  # type: ignore
        position = -1
        while response:
            items_json = response.json()
            media_json = items_json.get("mediaItems")
            # cope with empty albums
            if not media_json:
                if not items_json.get("nextPageToken"):
                    break
                else:
                    media_json = []
                    log.warning("*** Empty Media JSON with a Next Page Token")

            for media_item_json in media_json:
                position += 1
                media_item = GooglePhotosMedia(media_item_json)

                if (not self.include_video) and media_item.is_video:
                    log.debug("---- skipping %s (--skip-video)", media_item.filename)
                    continue

                log.debug("----%s", media_item.filename)
                self._db.put_album_file(album_id, media_item.id, position)
                last_date = max(media_item.create_date, last_date)
                first_date = min(media_item.create_date, first_date)

                # this adds other users photos from shared albums
                # Todo - This will cause two copies of a file to appear for
                #  those shared items you have imported into your own library.
                #  They will have different RemoteIds, one will point to your
                #  library copy (you own) and one to the shared item in the
                #  the folder. Currently with the meta data available it would
                #  be impossible to eliminate these without eliminating other
                #  cases where date and filename (TITLE) match
                if add_media_items:
                    media_item.set_path_by_date(
                        self._photos_folder, self._use_flat_path
                    )
                    (num, _) = self._db.file_duplicate_no(
                        str(media_item.filename),
                        str(media_item.relative_folder),
                        media_item.id,
                    )
                    # we just learned if there were any duplicates in the db
                    media_item.duplicate_number = num

                    log.debug(
                        "Adding album media item %s %s %s",
                        media_item.relative_path,
                        media_item.filename,
                        media_item.duplicate_number,
                    )
                    self._db.put_row(GooglePhotosRow.from_media(media_item), False)

            next_page = items_json.get("nextPageToken")
            if next_page:
                body = self.make_search_parameters(
                    album_id=album_id, page_token=next_page
                )
                response = self._api.mediaItems.search.execute(body)  # type: ignore
            else:
                break
        return first_date, last_date

    def index_album_media(self):
        # we now index all contents of non-shared albums due to the behaviour
        # reported here https://github.com/gilesknap/gphotos-sync/issues/89
        if self.shared_albums:
            self.index_albums_type(
                self._api.sharedAlbums.list.execute,
                "sharedAlbums",
                "Shared (titled) Albums",
                False,
                True,
            )
        self.index_albums_type(
            self._api.albums.list.execute, "albums", "Albums", True, self.album_index
        )

    def index_albums_type(
        self,
        api_function: Callable,
        item_key: str,
        description: str,
        allow_null_title: bool,
        add_media_items: bool,
    ):
        """
        query google photos interface for a list of all albums and index their
        contents into the db
        """
        log.warning("Indexing {} ...".format(description))

        # when only looking for favourites do not download album contents
        if self.favourites:
            add_media_items = False

        # there are no filters in album listing at present so it always a
        # full rescan - it's quite quick
        count = 0
        response = api_function(pageSize=ALBUM_ITEMS)
        while response:
            results = response.json()
            for album_json in results.get(item_key, []):
                count += 1

                album = GoogleAlbumMedia(album_json)
                indexed_album = self._db.get_album(album_id=album.id)
                already_indexed = (
                    indexed_album.size == album.size if indexed_album else False
                )

                if self.album and self.album != album.orig_name:
                    log.debug(
                        "Skipping Album: %s, photos: %d " "(does not match --album)",
                        album.filename,
                        album.size,
                    )
                elif self.album_regex and not re.search(
                    self.album_regex, album.orig_name, re.I
                ):
                    log.debug(
                        "Skipping Album: %s, photos: %d "
                        "(does not match --album-regex)",
                        album.filename,
                        album.size,
                    )
                elif not allow_null_title and album.description == "none":
                    log.debug("Skipping no-title album, photos: %d", album.size)
                elif already_indexed and not self.flush:
                    log.debug(
                        "Skipping Album: %s, photos: %d", album.filename, album.size
                    )
                else:
                    log.info(
                        "Indexing Album: %s, photos: %d", album.filename, album.size
                    )
                    first_date, last_date = self.fetch_album_contents(
                        album.id, add_media_items
                    )
                    # write the album data down now we know the contents'
                    # date range
                    gar = GoogleAlbumsRow.from_parm(
                        album.id,
                        album.filename,
                        album.size,
                        first_date,
                        last_date,
                        {"albums": False, "sharedAlbums": True}.get(item_key),
                    )
                    self._db.put_row(gar, update=indexed_album)

                if self.settings.progress and count % 10 == 0:
                    log.warning(f"Listed {count} {description} ...\033[F")

            next_page = results.get("nextPageToken")
            if next_page:
                response = api_function(pageSize=ALBUM_ITEMS, pageToken=next_page)
            else:
                break
        log.warning("Indexed %d %s", count, description)

    def album_folder_name(
        self, album_name: str, start_date: datetime, end_date: datetime, shared: bool
    ) -> Path:
        album_name = get_check().valid_file_name(album_name)
        if self._omit_album_date:
            rel_path = album_name
        else:
            if self.use_start_date:
                d = start_date
            else:
                d = end_date
            year = Utils.safe_str_time(d, "%Y")
            month = Utils.safe_str_time(d, self.month_format or "%m%d")

            if self._use_flat_path:
                fmt = self.path_format or "{0}-{1} {2}"
                rel_path = fmt.format(year, month, album_name)
            else:
                fmt = self.path_format or "{0} {1}"
                rel_path = str(Path(year) / fmt.format(month, album_name))

        link_folder: Path
        if shared:
            link_folder = self._shared_albums_root / rel_path
        else:
            link_folder = self._albums_root / rel_path
        return link_folder

    def create_album_content_links(self):
        log.warning("Creating album folder links to media ...")
        count = 0
        album_item = 0
        current_rid = ""

        # Only remove and recreate all album links if not preserving links
        if not self._preserve_album_links:
            # always re-create all album links - it is quite fast and a good way
            # to ensure consistency
            # especially now that we have --album-date-by-first-photo
            if self._albums_root.exists():
                log.debug("removing previous album links tree")
                shutil.rmtree(self._albums_root)
            if self._shared_albums_root.exists():
                log.debug("removing previous shared album links tree")
                shutil.rmtree(self._shared_albums_root)
        
        # Create album directory structure if it doesn't exist
        if not self._albums_root.exists():
            self._albums_root.mkdir(parents=True, exist_ok=True)
        if not self._shared_albums_root.exists():
            self._shared_albums_root.mkdir(parents=True, exist_ok=True)
            
        re_download = not self._albums_root.exists()

        # When preserving album links, keep track of valid links to detect ones to remove later
        valid_links = set() if self._preserve_album_links else None

        for (
            path,
            file_name,
            album_name,
            start_date_str,
            end_date_str,
            rid,
            created,
            sharedAlbum,
        ) in self._db.get_album_files(
            album_invert=self._album_invert, download_again=re_download
        ):
            if current_rid == rid:
                album_item += 1
            else:
                self._db.put_album_downloaded(rid)
                current_rid = rid
                album_item = 0
            end_date = Utils.string_to_date(end_date_str)
            start_date = Utils.string_to_date(start_date_str)

            if len(str(self._root_folder / path)) > get_check().max_path:
                max_path_len = get_check().max_path - len(str(self._root_folder))
                log.debug(
                    "This path needs to be shrunk: %s" % Path(self._root_folder / path)
                )
                path = path[:max_path_len]
                log.debug("Shrunk to: %s" % Path(self._root_folder / path))

            file_name = file_name[: get_check().max_filename]

            full_file_name = self._root_folder / path / file_name

            link_folder: Path = self.album_folder_name(
                album_name, start_date, end_date, sharedAlbum
            )

            if self._no_album_sorting:
                link_filename = "{}".format(file_name)
            else:
                link_filename = "{:04d}_{}".format(album_item, file_name)

            link_filename = link_filename[: get_check().max_filename]
            link_file = link_folder / link_filename
            
            # Keep track of valid links when preserving
            if self._preserve_album_links:
                valid_links.add(str(link_file))
                
            # incredibly, pathlib.Path.relative_to cannot handle
            # '../' in a relative path !!! reverting to os.path
            relative_filename = os.path.relpath(full_file_name, str(link_folder))
            log.debug("adding album link %s -> %s", relative_filename, link_file)
            try:
                if not link_folder.is_dir():
                    log.debug("new album folder %s", link_folder)
                    link_folder.mkdir(parents=True)

                created_date = Utils.string_to_date(created)
                if full_file_name.exists():
                    # When preserving album links, check if the link already exists and points to the right target
                    create_link = True
                    if self._preserve_album_links and link_file.exists():
                        # Check if it's a symlink or hardlink to the correct target
                        if self._use_hardlinks:
                            # For hardlinks we check if both exist and have same inode
                            if link_file.exists() and full_file_name.exists() and \
                               link_file.stat().st_ino == full_file_name.stat().st_ino:
                                create_link = False
                        else:
                            # For symlinks, check if it points to the correct relative path
                            try:
                                if link_file.is_symlink() and os.readlink(str(link_file)) == relative_filename:
                                    create_link = False
                            except (OSError, ValueError):
                                # If the link is broken, we'll recreate it
                                pass
                    
                    # Create or recreate the link if needed
                    if create_link:
                        # Remove existing link if it exists
                        if link_file.exists():
                            if link_file.is_symlink() or self._use_hardlinks:
                                link_file.unlink()
                            else:
                                log.warning(f"Could not update link {link_file}: not a link")
                                
                        # Create the link
                        if self._use_hardlinks:
                            os.link(full_file_name, link_file)
                        elif self._ntfs_override:
                            os.symlink(relative_filename, link_file)
                        else:
                            link_file.symlink_to(relative_filename)
                else:
                    log.debug("skip link for %s, not downloaded", file_name)

                if link_file.exists():
                    count += 1
                    # Windows tries to follow symlinks even though we specify
                    # follow_symlinks=False. So disable setting of link date
                    # if follow not supported
                    try:
                        if os.utime in os.supports_follow_symlinks:
                            os.utime(
                                str(link_file),
                                (
                                    Utils.safe_timestamp(created_date).timestamp(),
                                    Utils.safe_timestamp(created_date).timestamp(),
                                ),
                                follow_symlinks=False,
                            )
                    except PermissionError:
                        log.debug(f"can't set date on {link_file}")

            except FileExistsError as err:
                log.info("duplicate link to %s: %s", full_file_name, err)
            except UnicodeEncodeError as err:
                log.error("unicode error linking %s: %s", full_file_name, err)

        # When preserving links, remove any links that no longer should exist
        removed_count = 0
        if self._preserve_album_links and valid_links:
            # Process regular albums
            if self._albums_root.exists():
                removed_count += self._clean_up_stale_links(self._albums_root, valid_links)
            # Process shared albums
            if self._shared_albums_root.exists():
                removed_count += self._clean_up_stale_links(self._shared_albums_root, valid_links)

        if self._preserve_album_links:
            log.warning("Created or updated %d album folder links, removed %d stale links", count, removed_count)
        else:
            log.warning("Created %d new album folder links", count)

    def _clean_up_stale_links(self, root_dir: Path, valid_links: set) -> int:
        """Removes links that no longer should exist in the album structure.
        
        Args:
            root_dir: The root directory to search for links
            valid_links: Set of validated link paths (as strings)
            
        Returns:
            Number of removed links
        """
        removed_count = 0
        
        # Recursively process each directory
        for path in root_dir.glob('**/*'):
            # Skip directories
            if path.is_dir():
                continue
                
            # If link isn't in our valid_links set and it's a symlink or hardlink, remove it
            if str(path) not in valid_links:
                try:
                    if path.is_symlink() or (self._use_hardlinks and path.exists()):
                        log.debug(f"Removing stale link: {path}")
                        path.unlink()
                        removed_count += 1
                except (OSError, PermissionError) as err:
                    log.error(f"Error removing stale link {path}: {err}")
                    
        # Remove empty directories
        for path in sorted(root_dir.glob('**/*'), key=lambda p: len(str(p)), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                try:
                    log.debug(f"Removing empty directory: {path}")
                    path.rmdir()
                except (OSError, PermissionError) as err:
                    log.error(f"Error removing empty directory {path}: {err}")
        
        return removed_count
