"""The ad commands: who goes live, who waits, what counts as an image, and
that nothing unapproved can reach the stream."""

import asyncio
import struct
import zlib

from fastapi.testclient import TestClient

from mpr import ads as adrules
from mpr import bot as B
from mpr import overlay
from tests.harness import Interaction, build_bot, click, edited_text, run

CREW = dict(user_id=10, name="JaeAIK", staff=True)
ANA, ROB = dict(user_id=20, name="ana"), dict(user_id=30, name="rob")


def png(width=24, height=4, rgb=(255, 168, 30)) -> bytes:
    """A real, tiny PNG, built by hand so the tests need no image library."""
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    row = b"\x00" + bytes(rgb) * width
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b""))


class Upload:
    """Stands in for a discord.Attachment."""

    def __init__(self, data: bytes, size: int | None = None):
        self.data, self.size, self.reads = data, len(data) if size is None else size, 0

    async def read(self):
        self.reads += 1
        return self.data


def live_headlines():
    return [s["headline"] for s in overlay.load_ads()["slots"]]


def test_crew_ads_go_live_straight_away(fresh_db):
    async def go():
        bot = await build_bot()
        out = await run(bot, "ad add", Interaction(**CREW), headline="Crew special",
                        body="From the booth", weight=4)
        assert "in the rotation" in out.text and out.sent[0]["ephemeral"]

    asyncio.run(go())
    assert "Crew special" in live_headlines()
    slot = next(s for s in overlay.load_ads()["slots"] if s["headline"] == "Crew special")
    assert slot["weight"] == 4


def test_viewer_ads_wait_for_the_crew_and_never_reach_the_stream_early(fresh_db):
    db = fresh_db
    upload = Upload(png())

    async def go():
        bot = await build_bot()
        out = await run(bot, "ad add", Interaction(**ANA), headline="Ana's Kart Wash",
                        image=upload, weight=9, tag="sneaky")
        assert "with the crew for review" in out.text
        assert not out.sent[0]["ephemeral"], "announced so the crew sees it come in"

    asyncio.run(go())
    a = db.ads(status="pending")[0]
    assert a["weight"] == 1 and a["tag"] == "made by ana"     # viewers can't set these
    assert "Ana's Kart Wash" not in live_headlines()
    client = TestClient(overlay.app)
    assert client.get(f"/ads/uploaded/{a['id']}").status_code == 404


def test_crew_review_approves_with_the_image_attached(fresh_db):
    db = fresh_db
    image = png()

    async def go():
        bot = await build_bot()
        await run(bot, "ad add", Interaction(**ANA), headline="Ana's Kart Wash", image=Upload(image))
        await run(bot, "ad add", Interaction(**ROB), headline="Rob's Rides")

        nosy = await run(bot, "review-ads", Interaction(**ANA))
        assert "for the crew" in nosy.text

        review = await run(bot, "review-ads", Interaction(**CREW))
        sent = review.sent[0]
        assert "2 waiting" in sent["content"] and sent["ephemeral"]
        assert sent["file"] is not None and sent["embed"].image.url.startswith("attachment://")

        stranger = await click(sent["view"], "Approve", Interaction(**ANA))
        assert "Crew only." in stranger.text
        done = await click(sent["view"], "Approve", Interaction(**CREW))
        assert "Approved" in edited_text(done) and "1 more waiting" in edited_text(done)

        second = await run(bot, "review-ads", Interaction(**CREW))
        gone = await click(second.sent[0]["view"], "Reject", Interaction(**CREW))
        assert "Rejected" in edited_text(gone) and "Queue's empty" in edited_text(gone)

    asyncio.run(go())
    assert "Ana's Kart Wash" in live_headlines() and "Rob's Rides" not in live_headlines()
    slot = next(s for s in overlay.load_ads()["slots"] if s["headline"] == "Ana's Kart Wash")
    got = TestClient(overlay.app).get(slot["image_url"])
    assert got.status_code == 200 and got.content == image
    assert got.headers["content-type"] == "image/png"
    assert db.ads(status="pending") == []


def test_only_real_images_under_4mb_are_accepted(fresh_db):
    async def go():
        bot = await build_bot()
        fake = await run(bot, "ad add", Interaction(**ANA), headline="Totally a picture",
                         image=Upload(b"MZ\x90\x00 not an image at all"))
        assert "isn't a PNG, JPG, GIF or WebP" in fake.text

        huge = Upload(png(), size=adrules.MAX_IMAGE_BYTES + 1)
        big = await run(bot, "ad add", Interaction(**ANA), headline="Huge", image=huge)
        assert "over 4 MB" in big.text and huge.reads == 0      # never downloaded

    asyncio.run(go())


def test_the_formats_the_rules_promise_are_recognised():
    assert adrules.sniff_image(png()) == "image/png"
    assert adrules.sniff_image(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert adrules.sniff_image(b"GIF89a....") == "image/gif"
    assert adrules.sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert adrules.sniff_image(b"%PDF-1.7") is None


def test_one_person_cant_flood_the_queue(fresh_db):
    async def go():
        bot = await build_bot()
        for i in range(adrules.MAX_PENDING_PER_PERSON):
            await run(bot, "ad add", Interaction(**ANA), headline=f"Ad {i}")
        extra = await run(bot, "ad add", Interaction(**ANA), headline="One more")
        assert "already have 3 ads waiting" in extra.text
        crew = await run(bot, "ad add", Interaction(**CREW), headline="Crew is never capped")
        assert "in the rotation" in crew.text

    asyncio.run(go())


def test_who_can_remove_what(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        await run(bot, "ad add", Interaction(**ANA), headline="Ana's")
        await run(bot, "ad add", Interaction(**ROB), headline="Rob's")
        ana_id = str(db.ads(submitted_by=20)[0]["id"])
        rob_id = str(db.ads(submitted_by=30)[0]["id"])

        blocked = await run(bot, "ad remove", Interaction(**ROB), ad=ana_id)
        assert "only remove ads you sent in" in blocked.text
        own = await run(bot, "ad remove", Interaction(**ROB), ad=rob_id)
        assert "Removed" in own.text
        crew = await run(bot, "ad remove", Interaction(**CREW), ad=ana_id)
        assert "Removed" in crew.text

        mine = await B.ad_options(Interaction(**ANA), "")
        assert mine == []                                   # viewers only see their own
        everything = await B.ad_options(Interaction(**CREW), "")
        assert len(everything) == 4                         # the four sample ads remain

    asyncio.run(go())


def test_text_is_cleaned_before_it_can_touch_the_layout(fresh_db):
    db = fresh_db

    async def go():
        bot = await build_bot()
        await run(bot, "ad add", Interaction(**CREW), headline="Line one\nline two\t\x07",
                  body="x" * 500)
        bad = await run(bot, "ad add", Interaction(**CREW), headline="Colour", accent="purple")
        assert "hex code" in bad.text
        ok = await run(bot, "ad add", Interaction(**CREW), headline="Colour", accent="5865F2")
        assert "in the rotation" in ok.text

    asyncio.run(go())
    rows = {a["headline"]: a for a in db.ads()}
    assert "Line one line two" in rows and len(rows["Line one line two"]["body"]) == adrules.MAX_BODY
    assert rows["Colour"]["accent"] == "#5865f2"


def test_the_sample_ads_are_imported_once_and_stay_gone_when_removed(fresh_db):
    db = fresh_db
    assert len(db.ads()) == 4
    for a in db.ads():
        db.remove_ad(a["id"])
    db.init()                                               # a restart
    assert db.ads() == []


def test_ad_list_shows_crew_the_queue_and_viewers_their_own(fresh_db):
    async def go():
        bot = await build_bot()
        await run(bot, "ad add", Interaction(**ANA), headline="Ana's pitch")
        crew = await run(bot, "ad list", Interaction(**CREW))
        assert "In the rotation" in crew.text and "Waiting for review (1)" in crew.text
        viewer = await run(bot, "ad list", Interaction(**ANA))
        assert "Ana's pitch" in viewer.text and "waiting for review" in viewer.text
        assert "Join the Discord" not in viewer.text

    asyncio.run(go())


def test_uploaded_images_need_the_overlay_key_too(fresh_db, monkeypatch):
    async def go():
        bot = await build_bot()
        await run(bot, "ad add", Interaction(**CREW), headline="Pic", image=Upload(png()))

    asyncio.run(go())
    monkeypatch.setattr(overlay, "OVERLAY_KEY", "k")
    url = next(s["image_url"] for s in overlay.load_ads()["slots"] if s["headline"] == "Pic")
    client = TestClient(overlay.app)
    assert client.get(url).status_code == 403
    assert client.get(url + "?key=k").status_code == 200
