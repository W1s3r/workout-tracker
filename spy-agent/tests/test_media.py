"""Images (paste, files, read_file) and voice (record + transcribe)."""
import base64
import io
import struct
import sys
import unittest
import wave
from pathlib import Path
from unittest import mock

from spy import images, voice
from spy.llm import make_client
from tests import test_agent
from tests.fakes import FakeLLMServer
from tests.helpers import make_config, tmpdir

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None


def pixels(png_bytes):
    return list(Image.open(io.BytesIO(png_bytes)).convert("RGB").getdata())


def make_dib(width, height, rgb_rows, bpp=32, top_down=False):
    """Build a CF_DIB blob like the Windows clipboard provides (rows given top-down)."""
    step = bpp // 8
    stride = ((width * bpp + 31) // 32) * 4
    data = b""
    order = rgb_rows if top_down else list(reversed(rgb_rows))
    for row in order:
        line = b"".join(bytes([b, g, r] + ([0] if step == 4 else [])) for (r, g, b) in row)
        data += line + b"\0" * (stride - len(line))
    hdr = struct.pack("<IiiHHIIiiII", 40, width, -height if top_down else height, 1, bpp, 0, len(data), 0, 0, 0, 0)
    return hdr + data


RED, GREEN, BLUE, WHITE = (220, 20, 20), (20, 200, 20), (20, 20, 220), (255, 255, 255)


@unittest.skipIf(Image is None, "Pillow needed to verify decoded pixels")
class TestImageCodec(unittest.TestCase):
    def test_png_encode(self):
        png = images.png_encode(2, 2, [bytes(RED + GREEN), bytes(BLUE + WHITE)])
        self.assertEqual(images.png_size(png), (2, 2))
        self.assertEqual(pixels(png), [RED, GREEN, BLUE, WHITE])

    def test_dib_32bit_bottom_up_and_24bit_padded(self):
        rows = [[RED, GREEN, BLUE], [WHITE, RED, GREEN]]
        for bpp in (32, 24):
            for top_down in (False, True):
                png = images.dib_to_png(make_dib(3, 2, rows, bpp, top_down))
                self.assertEqual(pixels(png), rows[0] + rows[1], (bpp, top_down))

    def test_data_url_downscales_and_converts(self):
        d = tmpdir()
        big = d / "big.png"
        Image.new("RGB", (4000, 1000), RED).save(big)
        url = images.image_data_url(big, max_px=1000)
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertEqual(Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).size, (1000, 250))
        bmp = d / "a.bmp"
        Image.new("RGB", (10, 10), RED).save(bmp)
        self.assertTrue(images.image_data_url(bmp).startswith("data:image/png"))

    def test_data_url_without_pillow(self):
        d = tmpdir()
        p = d / "s.png"
        p.write_bytes(images.png_encode(1, 1, [bytes(RED)]))
        with mock.patch.dict(sys.modules, {"PIL": None}):
            url = images.image_data_url(p)
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), p.read_bytes())


class TestImagesInConversation(unittest.TestCase):
    make = test_agent.AgentTest.make

    def _png(self, name="shot.png"):
        p = self.dir / name
        p.write_bytes(images.png_encode(2, 2, [bytes(RED + RED)] * 2))
        return p

    @staticmethod
    def image_parts(msg):
        c = msg.get("content")
        return [x for x in c if x.get("type") == "image_url"] if isinstance(c, list) else []

    def test_pasted_image_sent_as_image_part(self):
        a = self.make([{"content": "It shows a red square."}])
        out = a.run_turn("what is in this screenshot?", images=[str(self._png())])
        self.assertIn("red square", out)
        user = self.srv.requests[0]["messages"][-1]
        self.assertEqual(user["content"][0], {"type": "text", "text": "what is in this screenshot?"})
        self.assertTrue(self.image_parts(user)[0]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_text_only_model_gets_note_not_image(self):
        a = self.make([{"content": "ok"}])
        a.llm.supports_vision = False
        a.run_turn("look", images=[str(self._png())])
        user = self.srv.requests[0]["messages"][-1]
        self.assertIsInstance(user["content"], str)
        self.assertIn("cannot view images", user["content"])

    def test_read_file_on_image_shows_it_to_model(self):
        a = self.make([{"tool_calls": [{"name": "read_file", "arguments": {"path": "chart.png"}}]},
                       {"content": "The chart is red."}], mode="auto")
        self._png("chart.png")
        a.run_turn("describe chart.png")
        msgs = self.srv.requests[1]["messages"]
        self.assertEqual(msgs[-2]["role"], "tool")
        self.assertIn("attached below", msgs[-2]["content"])
        self.assertEqual(msgs[-1]["role"], "user")
        self.assertEqual(len(self.image_parts(msgs[-1])), 1)

    def test_only_recent_images_resent(self):
        a = self.make([{"content": "ok"}] * 6)
        for i in range(6):
            a.run_turn("image %d" % i, images=[str(self._png("s%d.png" % i))])
        last = self.srv.requests[-1]["messages"]
        with_imgs = [m for m in last if self.image_parts(m)]
        self.assertEqual(len(with_imgs), 4)  # images.keep_last default
        self.assertIn("omitted to save context", [m for m in last if m["role"] == "user"][0]["content"])

    def test_prompted_mode_keeps_images(self):
        a = self.make([{"content": "red"}], stream=False)
        a.llm.tool_mode = "prompted"
        a.run_turn("what color is this?", images=[str(self._png())])
        self.assertEqual(len(self.image_parts(self.srv.requests[0]["messages"][-1])), 1)


class TestCliImageInput(unittest.TestCase):
    def setUp(self):
        import os
        self.dir = tmpdir()
        os.environ["SPY_HOME"] = str(self.dir / ".spyhome")
        from spy import cli
        with mock.patch("pathlib.Path.cwd", return_value=self.dir):
            self.app = cli.App(mock.Mock(config=None, model="mock", mode="auto"))
        self.app.agent.ctx.cwd = self.dir
        self.shot = self.dir / "shot.png"
        self.shot.write_bytes(images.png_encode(1, 1, [bytes(RED)]))

    def test_paste_tokens_mentions_and_dropped_paths(self):
        with mock.patch("spy.images.grab_clipboard", return_value=([self.shot], None)):
            labels, _ = self.app.paste_from_clipboard()
        self.assertEqual(labels, ["[Image #1]"])
        self.app.input.fancy = True
        text, imgs = self.app.prepare_message("what's wrong here [Image #1]")
        self.assertEqual(imgs, [str(self.shot)])
        self.assertEqual(self.app.pasted_images, {})
        other = self.dir / "two words.png"
        other.write_bytes(self.shot.read_bytes())
        text, imgs = self.app.prepare_message('compare @shot.png with "%s"' % other)
        self.assertEqual(sorted(imgs), sorted([str(self.shot), str(other)]))
        self.assertNotIn("<file", text)  # images are attached as images, not dumped as text

    def test_plain_mode_paste_attaches_to_next_message(self):
        self.app.input.fancy = False
        with mock.patch("spy.images.grab_clipboard", return_value=([self.shot], None)):
            self.app.slash("/paste")
        with mock.patch.object(self.app.agent, "run_turn", return_value="") as rt:
            self.app.handle("explain this error")
        self.assertEqual(rt.call_args.kwargs["images"], [str(self.shot)])

    def test_voice_puts_transcript_in_input(self):
        with mock.patch("spy.voice.record", return_value=self.shot), \
                mock.patch("spy.voice.transcribe", return_value=" refactor the parser please "):
            self.app.slash("/voice")
        self.assertEqual(self.app.input.prefill, "refactor the parser please")
        # plain mode: Enter sends the transcript as-is
        self.app.input.fancy = False
        with mock.patch("sys.stdin", io.StringIO("\n")):
            self.assertEqual(self.app.input.read(), "refactor the parser please")


def wav_bytes(seconds=0.5):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x01" * int(16000 * seconds))
    return buf.getvalue()


class TestVoice(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        self.wav = self.dir / "v.wav"
        self.wav.write_bytes(wav_bytes())

    def _llm(self, reply):
        srv = FakeLLMServer(lambda b: {"content": reply})
        self.addCleanup(srv.close)
        cfg = make_config(self.dir, providers={"work": {"base_url": srv.url, "api_key": "k", "max_retries": 0}},
                          models={"inkling-small": {"model": "fake-model", "stream": False, "context_tokens": 32000}})
        return srv, (lambda role: make_client(cfg.model_profile(role)))

    def test_llm_engine_input_audio(self):
        srv, factory = self._llm('"Open the quarterly report."')
        text = voice.transcribe(self.wav, {"engine": "llm"}, factory)
        self.assertEqual(text, "Open the quarterly report.")
        parts = srv.requests[0]["messages"][0]["content"]
        self.assertEqual(parts[1]["type"], "input_audio")
        self.assertEqual(base64.b64decode(parts[1]["input_audio"]["data"]), self.wav.read_bytes())
        self.assertEqual(srv.requests[0]["temperature"], 0)

    def test_llm_engine_audio_url(self):
        srv, factory = self._llm("hi")
        voice.transcribe(self.wav, {"engine": "llm", "audio_part": "audio_url"}, factory)
        part = srv.requests[0]["messages"][0]["content"][1]
        self.assertTrue(part["audio_url"]["url"].startswith("data:audio/wav;base64,"))

    def test_transcriptions_api(self):
        srv = FakeLLMServer(lambda b: {})
        self.addCleanup(srv.close)
        text = voice.transcribe(self.wav, {"engine": "transcriptions", "url": srv.url + "/audio/transcriptions",
                                           "model": "whisper-large", "language": "en"}, None)
        self.assertEqual(text, "hello from whisper")
        req = srv.requests[0]
        self.assertIn("multipart/form-data; boundary=", req["_ctype"])
        self.assertIn(b'name="model"\r\n\r\nwhisper-large', req["_multipart"])
        self.assertIn(b'name="language"\r\n\r\nen', req["_multipart"])
        self.assertIn(self.wav.read_bytes(), req["_multipart"])

    def test_command_engine(self):
        script = self.dir / "stt.py"
        script.write_text("import sys\nprint('transcribed ' + sys.argv[1].split('/')[-1].split(chr(92))[-1])\n")
        text = voice.transcribe(self.wav, {"engine": "command",
                                           "command": '"%s" "%s" {wav}' % (sys.executable, script)}, None)
        self.assertEqual(text, "transcribed v.wav")

    def test_record_with_command_recorder(self):
        fake_rec = self.dir / "fake_rec.py"
        fake_rec.write_text("import sys, time, wave\n"
                            "w = wave.open(sys.argv[1], 'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)\n"
                            "w.writeframes(b'\\x00\\x01' * 16000); w.close()\n"
                            "time.sleep(60)\n")
        calls = []

        def stop_after_three(timeout):
            calls.append(timeout)
            return len(calls) >= 3
        out = voice.record({"record_command": '"%s" "%s" {out}' % (sys.executable, fake_rec), "max_seconds": 30},
                           self.dir / "audio", stop_after_three)
        self.assertTrue(out.exists())
        self.assertAlmostEqual(voice.wav_seconds(out), 1.0, places=1)

    def test_nothing_recorded_is_an_error(self):
        script = self.dir / "noop.py"
        script.write_text("import time\ntime.sleep(60)\n")
        with self.assertRaises(voice.VoiceError):
            voice.record({"record_command": '"%s" "%s" {out}' % (sys.executable, script)}, self.dir / "a",
                         lambda t: True)


if __name__ == "__main__":
    unittest.main()
