# Background sounds

No sound files ship with this repo: the recordings used during development came from a stock site whose license forbids
redistributing them as files. The feature is off by default, and everything works without a sound.

To add your own (you need the rights to the audio, and ffmpeg):

- **Voice page:** upload an MP3 or WAV (5 s or longer, up to 20 MB). It is stored under the data folder.
- **Command line:** `python dashboard/backend/process_sounds.py my_recording.mp3 office` writes `app/sounds/office.wav`,
  which then appears on the Voice page as "Office".

Both go through the same pipeline (`app/ambience.py`): mono 16 kHz WAV, 200 Hz to 4 kHz band-pass so it sounds like a phone
line, loudness normalised to -26 LUFS, a limiter, a crossfaded loop, and a length of 2 to 3 minutes.
