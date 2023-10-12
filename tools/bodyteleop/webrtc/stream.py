import asyncio
import aiortc
from aiortc.contrib.media import MediaRelay

import abc
import argparse
import json
from typing import Callable, Awaitable, Dict, List, Any

from openpilot.tools.bodyteleop.webrtc.common import StreamingOffer, ConnectionProvider


class WebRTCStreamBuilder:
  def __init__(self):
    self.consumed_camera_tracks = set()
    self.consume_audio = False
    self.video_producing_tracks = []
    self.audio_producing_tracks = []
    self.data_channel = None
    self.message_handler = None
    self.peer_connection = None

  def add_video_consumer(self, camera_type):
    assert camera_type in ["driver", "wideRoad", "road"]

    self.consumed_camera_tracks.add(camera_type)

  def add_audio_consumer(self):
    if self.consume_audio:
      raise Exception("Only one audio consumer allowed")

    self.consume_audio = True

  def add_video_producer(self, track: aiortc.MediaStreamTrack):
    assert track.kind == "video"
    self.video_producing_tracks.append(track)

  def add_audio_producer(self, track: aiortc.MediaStreamTrack):
    assert track.kind == "audio"
    self.audio_producing_tracks.append(track)

  # def add_channel(self, message_handler: Callable[[DataChannelMessenger, Any], Awaitable[None]]):
  #   self.data_channel = DataChannelMessenger()
  #   self.data_channel.on_message(message_handler)

  def offer(self, connection_provider: Callable[[StreamingOffer], Awaitable[aiortc.RTCSessionDescription]]):
    return WebRTCOfferStream(connection_provider, self.consumed_camera_tracks, self.consume_audio, self.video_producing_tracks, self.audio_producing_tracks)

  def answer(self, offer: StreamingOffer):
    return WebRTCAnswerStream(offer, self.consumed_camera_tracks, self.consume_audio, self.video_producing_tracks, self.audio_producing_tracks)


class WebRTCBaseStream(abc.ABC):
  def __init__(self,
               consumed_camera_types: List[str],
               consume_audio: bool,
               video_producer_tracks: List[aiortc.MediaStreamTrack],
               audio_producer_tracks: List[aiortc.MediaStreamTrack]):
    self.peer_connection = aiortc.RTCPeerConnection()
    self.media_relay = MediaRelay()
    self.expected_incoming_camera_types = consumed_camera_types
    self.extected_incoming_audio = consume_audio
    self.incoming_camera_tracks = dict()
    self.incoming_audio_tracks = []
    self.outgoing_video_tracks = video_producer_tracks
    self.outgoing_audio_tracks = audio_producer_tracks

    self.peer_connection.on("connectionstatechange", self._on_connectionstatechange)
    self.peer_connection.on("datachannel", self._on_incoming_datachannel)
    self.peer_connection.on("track", self._on_incoming_track)

  def _add_consumer_tracks(self):
    for _ in self.expected_incoming_camera_types:
      self.peer_connection.addTransceiver("video", direction="recvonly")
    if self.extected_incoming_audio:
      self.peer_connection.addTransceiver("audio", direction="recvonly")
  
  def _add_producer_tracks(self):
    for track in self.outgoing_video_tracks:
      self.peer_connection.addTrack(track)
      # force codec?
    for track in self.outgoing_audio_tracks:
      self.peer_connection.addTrack(track)
    
  def _on_connectionstatechange(self):
    print("-- connection state is", self.peer_connection.connectionState)

  def _on_incoming_track(self, track):
    print("-- got track: ", track.kind, track.id)
    if track.kind == "video":
      parts = track.id.split(":") # format: "camera_type:camera_id"
      if len(parts) < 2:
        return

      camera_type = parts[0]
      if camera_type in self.expected_incoming_camera_types:
        self.incoming_camera_tracks[camera_type] = track
    elif track.kind == "audio":
      if self.expected_incoming_audio:
        self.incoming_audio_tracks.append(track)

  def _on_incoming_datachannel(self, channel):
    print("-- got data channel: ", channel.label)

  def get_incoming_video_track(self, camera_type: str, buffered: bool):
    assert camera_type in self.incoming_camera_tracks

    track = self.incoming_camera_tracks[camera_type]
    relay_track = self.media_relay.subscribe(track, buffered=buffered)
    return relay_track
  
  def get_incoming_audio_track(self, buffered: bool):
    assert len(self.incoming_audio_tracks) > 0

    track = self.incoming_audio_tracks[0]
    relay_track = self.media_relay.subscribe(track, buffered=buffered)
    return relay_track
  
  @abc.abstractmethod
  async def start(self):
    raise NotImplemented()


class WebRTCOfferStream(WebRTCBaseStream):
  def __init__(self, session_provider: ConnectionProvider, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self.session_provider = session_provider
    self._add_producer_tracks()

  async def start(self):
    self._add_consumer_tracks()

    offer = await self.peer_connection.createOffer()
    await self.peer_connection.setLocalDescription(offer)
    actual_offer = self.peer_connection.localDescription

    streaming_offer = StreamingOffer(sdp=actual_offer.sdp, 
                                           type=actual_offer.type, 
                                           video=list(self.expected_incoming_camera_types), 
                                           audio=self.extected_incoming_audio)
    remote_answer = await self.session_provider(streaming_offer)
    await self.peer_connection.setRemoteDescription(remote_answer)
    actual_answer = self.peer_connection.remoteDescription
    # wait for the tracks to be ready
    #await self.tracks_ready_event.wait()

    return actual_answer


class WebRTCAnswerStream(WebRTCBaseStream):
  def __init__(self, offer: StreamingOffer, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self.offer = offer
  
  async def start(self):
    assert self.peer_connection.remoteDescription is None, "Connection already established"

    await self.peer_connection.setRemoteDescription(self.offer)

    self._add_consumer_tracks()
    self._add_producer_tracks()

    answer = await self.peer_connection.createAnswer()
    await self.peer_connection.setLocalDescription(answer)
    actual_answer = self.peer_connection.localDescription

    return actual_answer

if __name__ == "__main__":
  from openpilot.tools.bodyteleop.webrtc.tracks import LiveStreamVideoStreamTrack, FrameReaderVideoStreamTrack
  from openpilot.tools.bodyteleop.webrtc.common import StdioConnectionProvider

  parser = argparse.ArgumentParser()
  subparsers = parser.add_subparsers(dest="command", required=True)
  offer_parser = subparsers.add_parser("offer")
  offer_parser.add_argument("cameras", metavar="CAMERA", type=str, nargs="+", default=["driver"], help="Camera types to stream")

  answer_parser = subparsers.add_parser("answer")
  answer_parser.add_argument("--input-video", type=str, required=False, help="Stream from video file instead")
  
  args = parser.parse_args()

  async def async_input():
    return await asyncio.to_thread(input)

  async def run_answer(args):
    streams = []
    while True:
      print("-- Please enter a JSON from client --")
      raw_payload = await async_input()
      
      payload = json.loads(raw_payload)
      offer = StreamingOffer(**payload)
      video_tracks = []
      for cam in offer.video:
        if args.input_video:
          track = FrameReaderVideoStreamTrack(args.input_video, camera_type=cam)
        else:
          track = LiveStreamVideoStreamTrack(cam)
        video_tracks.append(track)
      audio_tracks = []

      stream_builder = WebRTCStreamBuilder()
      for track in video_tracks:
        stream_builder.add_video_producer(track)
      for track in audio_tracks:
        stream_builder.add_audio_producer(track)
      stream = stream_builder.answer(offer)
      answer = await stream.start()
      streams.append(stream)

      print("-- Please send this JSON to client --")
      print(json.dumps({"sdp": answer.sdp, "type": answer.type}))

  async def run_offer(args):
    connection_provider = StdioConnectionProvider()
    stream_builder = WebRTCStreamBuilder()
    for cam in args.cameras:
      stream_builder.add_video_consumer(cam)
    stream = stream_builder.offer(connection_provider)
    _ = await stream.start()

    # TODO wait for the tracks to be ready using events
    asyncio.sleep(2)
    tracks = [stream.get_incoming_video_track(cam, False) for cam in args.cameras]
    while True:
      try:
        frames = await asyncio.gather(*[track.recv() for track in tracks])
        for key, frame in zip(args.cameras, frames):
          print("Received frame from", key, frame.shape)
      except aiortc.mediastreams.MediaStreamError:
        return
      print("=====================================")

  loop = asyncio.get_event_loop()
  if args.command == "offer":
    loop.run_until_complete(run_offer(args))
  elif args.command == "answer":
    loop.run_until_complete(run_answer(args))
