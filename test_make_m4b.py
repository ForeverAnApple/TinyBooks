import tempfile
import unittest
from pathlib import Path

import make_m4b


class MakeM4BTests(unittest.TestCase):
    def test_kazin_title_prefix_is_stripped(self):
        self.assertEqual(
            make_m4b._strip_kazin_title_prefix("Culture Book 5 - Excession"),
            "Excession",
        )

    def test_recursive_disc_layout_preserves_disc_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in (
                "Disc 02/Track 02.mp3",
                "Disc 01/Track 02.mp3",
                "Disc 10/Track 01.mp3",
                "Disc 02/Track 01.mp3",
                "Disc 01/Track 01.mp3",
            ):
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()

            self.assertEqual(
                [str(p.relative_to(root)) for p in make_m4b.find_audio_files(root)],
                [
                    "Disc 01/Track 01.mp3",
                    "Disc 01/Track 02.mp3",
                    "Disc 02/Track 01.mp3",
                    "Disc 02/Track 02.mp3",
                    "Disc 10/Track 01.mp3",
                ],
            )
            layout, payload = make_m4b.detect_layout(root)
            self.assertEqual(layout, "multi_file")
            self.assertEqual(len(payload), 5)

    def test_duplicate_nested_track_titles_include_disc_name(self):
        files = [
            Path("Disc 01/Track 01.mp3"),
            Path("Disc 01/Track 02.mp3"),
            Path("Disc 02/Track 01.mp3"),
            Path("Disc 02/Track 02.mp3"),
        ]
        chapters = make_m4b.build_chapters("multi_file", files, lambda _: 0)
        self.assertEqual(
            [chapter[0] for chapter in chapters],
            [
                "Disc 01 - Track 01",
                "Disc 01 - Track 02",
                "Disc 02 - Track 01",
                "Disc 02 - Track 02",
            ],
        )

    def test_loose_mp3_input_uses_matching_cue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "Book.mp3"
            cue = root / "Book.cue"
            audio.touch()
            cue.touch()

            layout, payload = make_m4b.detect_layout(audio)
            self.assertEqual(layout, "single_file_with_cue")
            self.assertEqual(payload, (audio, cue))

    def test_series_metadata_emits_abs_tags(self):
        metadata = make_m4b.build_metadata(
            "Excession",
            "Iain M. Banks",
            [("Chapter 1", Path("01.mp3"), None, None)],
            [1.0],
            series="Culture",
            series_part="5",
        )
        self.assertIn("album=Excession: Culture, Book 5", metadata)
        self.assertIn("album_artist=Iain M. Banks", metadata)
        self.assertIn(r"grouping=Culture \#5", metadata)
        self.assertIn("track=1/1", metadata)

    def test_required_metadata_treats_unknown_author_and_missing_cover_as_missing(self):
        self.assertEqual(
            make_m4b.metadata_missing(
                title="Book",
                author="Unknown Author",
                narrator="Narrator",
                year="2024",
                genre="Fiction",
                description="Description",
                cover_art=None,
            ),
            ["author", "cover art"],
        )

    def test_existing_output_requires_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "Book.m4b"
            output.touch()
            with self.assertRaises(SystemExit):
                make_m4b.validate_output_path(output, force=False)
            make_m4b.validate_output_path(output, force=True)


if __name__ == "__main__":
    unittest.main()
