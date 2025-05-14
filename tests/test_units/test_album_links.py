import logging
import os
import shutil
from pathlib import Path
from unittest import TestCase, mock

import pytest

from gphotos_sync import Utils
from gphotos_sync.Checks import do_check, get_check
from gphotos_sync.GoogleAlbumsSync import GoogleAlbumsSync
from gphotos_sync.LocalData import LocalData
from gphotos_sync.restclient import RestClient
from gphotos_sync.Settings import Settings

# Set up logging
logging.basicConfig(level=logging.DEBUG)


class TestAlbumLinks(TestCase):
    def setUp(self):
        # Create a temp directory for testing
        self.test_dir = Path("test_album_links_dir").absolute()
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.test_dir.mkdir()

        # Initialize the Checks module
        do_check(self.test_dir)

        # Create album directories
        self.albums_dir = self.test_dir / "albums"
        self.albums_dir.mkdir()
        self.shared_albums_dir = self.test_dir / "sharedAlbums"
        self.shared_albums_dir.mkdir()

        # Create a test photo
        self.photos_dir = self.test_dir / "photos"
        self.photos_dir.mkdir()
        self.photos_dir_2020 = self.photos_dir / "2020"
        self.photos_dir_2020.mkdir()
        self.test_photo = self.photos_dir_2020 / "test_photo.jpg"
        self.test_photo.touch()

        print(f"Test directory: {self.test_dir}")
        print(f"Test photo: {self.test_photo}")

    def tearDown(self):
        # Clean up
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)

    @mock.patch("gphotos_sync.LocalData.LocalData")
    @mock.patch("gphotos_sync.restclient.RestClient")
    def test_simple_link_creation(self, mock_rest_client, mock_local_data):
        """Test basic link creation without the complexity"""
        mock_db = mock_local_data.return_value
        # Format: path, file_name, album_name, start_date, end_date, rid, created, shared_album
        mock_db.get_album_files.return_value = [
            (
                str(Path("photos/2020")),
                "test_photo.jpg",
                "Test Album",
                Utils.date_to_string(Utils.string_to_date("2020-01-01")),
                Utils.date_to_string(Utils.string_to_date("2020-12-31")),
                "album1",
                Utils.date_to_string(Utils.string_to_date("2020-06-15")),
                False,
            )
        ]

        settings = Settings(
            start_date=Utils.string_to_date("2020-01-01"),
            end_date=Utils.string_to_date("2020-12-31"),
            use_start_date=False,
            photos_path=Path("photos"),
            use_flat_path=False,
            albums_path=Path("albums"),
            shared_albums_path=Path("sharedAlbums"),
            album_index=True,
            omit_album_date=False,
            album_invert=False,
            no_album_sorting=False,
            album="",
            album_regex="",
            shared_albums=True,
            favourites_only=False,
            include_video=True,
            archived=False,
            use_hardlinks=False,
            retry_download=False,
            rescan=False,
            max_retries=3,
            max_threads=5,
            case_insensitive_fs=False,
            progress=False,
            ntfs_override=False,
            month_format="%m",
            path_format=None,
            image_timeout=30,
            video_timeout=120,
            preserve_album_links=False,  # Don't use preserve links for this simple test
        )

        album_sync = GoogleAlbumsSync(
            mock_rest_client, self.test_dir, mock_db, False, settings
        )

        # Create the album content links
        album_sync.create_album_content_links()

        # Check what was created
        expected_album_dir = self.albums_dir / "2020" / "12 Test Album"
        expected_link_file = expected_album_dir / "0000_test_photo.jpg"

        print(f"Expected album dir: {expected_album_dir}")
        print(f"Expected link file: {expected_link_file}")
        print(f"Album dir exists: {expected_album_dir.exists()}")

        # List all files under the albums directory to see what's there
        print("Files in albums directory:")
        for path in self.albums_dir.glob("**/*"):
            print(f"  {path}")

        # Print debugging info about the mock data
        print("Mock get_album_files returned:")
        for item in mock_db.get_album_files.return_value:
            print(f"  {item}")

        self.assertTrue(expected_album_dir.exists())
        self.assertTrue(expected_link_file.exists())

    @mock.patch("gphotos_sync.LocalData.LocalData")
    @mock.patch("gphotos_sync.restclient.RestClient")
    def test_preserve_album_links(self, mock_rest_client, mock_local_data):
        """Test that preserve_album_links only updates links when needed"""
        # Mock DB response
        mock_db = mock_local_data.return_value
        # Format: path, file_name, album_name, start_date, end_date, rid, created, shared_album
        mock_db.get_album_files.return_value = [
            (
                str(Path("photos/2020")),
                "test_photo.jpg",
                "Test Album",
                Utils.date_to_string(Utils.string_to_date("2020-01-01")),
                Utils.date_to_string(Utils.string_to_date("2020-12-31")),
                "album1",
                Utils.date_to_string(Utils.string_to_date("2020-06-15")),
                False,
            )
        ]

        # Create settings with preserve_album_links=True
        settings = Settings(
            start_date=Utils.string_to_date("2020-01-01"),
            end_date=Utils.string_to_date("2020-12-31"),
            use_start_date=False,
            photos_path=Path("photos"),
            use_flat_path=False,
            albums_path=Path("albums"),
            shared_albums_path=Path("sharedAlbums"),
            album_index=True,
            omit_album_date=False,
            album_invert=False,
            no_album_sorting=False,
            album="",
            album_regex="",
            shared_albums=True,
            favourites_only=False,
            include_video=True,
            archived=False,
            use_hardlinks=False,
            retry_download=False,
            rescan=False,
            max_retries=3,
            max_threads=5,
            case_insensitive_fs=False,
            progress=False,
            ntfs_override=False,
            month_format="%m",
            path_format=None,
            image_timeout=30,
            video_timeout=120,
            preserve_album_links=True,
        )

        # Create album sync object
        album_sync = GoogleAlbumsSync(
            mock_rest_client, self.test_dir, mock_db, False, settings
        )

        # Create an album link (simulating a previous run)
        album_dir = self.albums_dir / "2020" / "12 Test Album"
        album_dir.mkdir(parents=True)
        link_file = album_dir / "0000_test_photo.jpg"

        # On non-Windows, create a symbolic link
        if os.name != "nt":
            relative_path = os.path.relpath(self.test_photo, album_dir)
            link_file.symlink_to(relative_path)
        else:
            # On Windows, just create a file to simulate a link
            link_file.touch()
            pytest.skip("Skipping symlink test on Windows")

        # Run create_album_content_links
        album_sync.create_album_content_links()

        # Verify the link was not recreated
        self.assertTrue(link_file.exists())

        # Now test removing a stale link
        stale_link = album_dir / "stale_link.jpg"
        if os.name != "nt":
            stale_link.symlink_to(relative_path)
        else:
            stale_link.touch()

        # Run create_album_content_links again
        album_sync.create_album_content_links()

        # Verify the stale link was removed
        self.assertFalse(stale_link.exists())

    @mock.patch("gphotos_sync.LocalData.LocalData")
    @mock.patch("gphotos_sync.restclient.RestClient")
    def test_hardlinks(self, mock_rest_client, mock_local_data):
        """Test that hardlinks are created correctly"""
        # Skip on Windows as the test is more complex there
        if os.name == "nt":
            pytest.skip("Skipping hardlink test on Windows")

        # Mock DB response
        mock_db = mock_local_data.return_value
        # Format: path, file_name, album_name, start_date, end_date, rid, created, shared_album
        mock_db.get_album_files.return_value = [
            (
                str(Path("photos/2020")),
                "test_photo.jpg",
                "Test Album",
                Utils.date_to_string(Utils.string_to_date("2020-01-01")),
                Utils.date_to_string(Utils.string_to_date("2020-12-31")),
                "album1",
                Utils.date_to_string(Utils.string_to_date("2020-06-15")),
                False,
            )
        ]

        # Create settings with use_hardlinks=True
        settings = Settings(
            start_date=Utils.string_to_date("2020-01-01"),
            end_date=Utils.string_to_date("2020-12-31"),
            use_start_date=False,
            photos_path=Path("photos"),
            use_flat_path=False,
            albums_path=Path("albums"),
            shared_albums_path=Path("sharedAlbums"),
            album_index=True,
            omit_album_date=False,
            album_invert=False,
            no_album_sorting=False,
            album="",
            album_regex="",
            shared_albums=True,
            favourites_only=False,
            include_video=True,
            archived=False,
            use_hardlinks=True,
            retry_download=False,
            rescan=False,
            max_retries=3,
            max_threads=5,
            case_insensitive_fs=False,
            progress=False,
            ntfs_override=False,
            month_format="%m",
            path_format=None,
            image_timeout=30,
            video_timeout=120,
            preserve_album_links=True,
        )

        # Create album sync object
        album_sync = GoogleAlbumsSync(
            mock_rest_client, self.test_dir, mock_db, False, settings
        )

        # Run create_album_content_links
        album_sync.create_album_content_links()

        # Verify the hardlink was created
        album_dir = self.albums_dir / "2020" / "12 Test Album"
        link_file = album_dir / "0000_test_photo.jpg"

        self.assertTrue(album_dir.exists())
        self.assertTrue(link_file.exists())

        # Verify it's a hardlink by checking inode numbers
        self.assertEqual(link_file.stat().st_ino, self.test_photo.stat().st_ino)
