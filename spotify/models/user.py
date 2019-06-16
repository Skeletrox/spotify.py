"""Source implementation for a spotify User"""

import asyncio
import functools
from typing import Optional, Dict, Union, List, Tuple

from ..utils import to_id
from ..http import HTTPUserClient
from . import URIBase, Image, Device, Context, Player, Playlist, Track, Artist, Library

REFRESH_TOKEN_URL = "https://accounts.spotify.com/api/token?grant_type=refresh_token&refresh_token={refresh_token}"


def ensure_http(func):
    func.__ensure_http__ = True
    return func


class User(URIBase):
    """A Spotify User.

    Attributes
    ----------
    id : :class:`str`
        The Spotify user ID for the user.
    uri : :class:`str`
        The Spotify URI for the user.
    url : :class:`str`
        The open.spotify URL.
    href : :class:`str`
        A link to the Web API endpoint for this user.
    display_name : :class:`str`
        The name displayed on the user’s profile.
        `None` if not available.
    followers : :class:`int`
        The total number of followers.
    images : List[:class:`Image`]
        The user’s profile image.
    email : :class:`str`
        The user’s email address, as entered by the user when creating their account.
    country : :class:`str`
        The country of the user, as set in the user’s account profile. An ISO 3166-1 alpha-2 country code.
    birthdate : :class:`str`
        The user’s date-of-birth.
    product : :class:`str`
        The user’s Spotify subscription level: “premium”, “free”, etc. 
        (The subscription level “open” can be considered the same as “free”.)
    """

    def __init__(self, client, data, **kwargs):
        self.__client = client

        try:
            self.http = kwargs.pop("http")
        except KeyError:
            pass
        else:
            self.library = Library(client, self)

        # Public user object attributes
        self.id = data.pop("id")
        self.uri = data.pop("uri")
        self.url = data.pop("external_urls").get("spotify", None)
        self.display_name = data.pop("display_name", None)
        self.href = data.pop("href")
        self.followers = data.pop("followers", {}).get("total", None)
        self.images = list(Image(**image) for image in data.pop("images", []))

        # Private user object attributes
        self.email = data.pop("email", None)
        self.country = data.pop("country", None)
        self.birthdate = data.pop("birthdate", None)
        self.product = data.pop("product", None)

    def __repr__(self):
        return '<spotify.User: "%s">' % (self.display_name or self.id)

    def __getattr__(self, key, value):
        try:
            value = object.__getattr__(self, key, value)
        except AttributeError as err:
            raise AttributeError from err

        if hasattr(value, "__ensure_http__") and not hasattr(self, "http"):

            @functools.wraps(value)
            def _raise(*args, **kwargs):
                raise AttributeError(
                    "User has not HTTP presence to perform API requests."
                )

            return _raise
        else:
            return value

    async def _get_top(self, klass, data) -> List[Union[Track, Artist]]:
        _str = {Artist: "artists", Track: "tracks"}[klass]
        data = {
            key: value
            for key, value in data.items()
            if key in ("limit", "offset", "time_range")
        }

        resp = await self.http.top_artists_or_tracks(_str[klass], **data)

        return [klass(self.__client, item) for item in resp["items"]]

    async def _refreshing_token(self, expires, token):
        while True:
            await asyncio.sleep(expires - 1)

            route = ("POST", REFRESH_TOKEN_URL.format(refresh_token=token))
            data = await self.client.http.request(
                route, content_type="application/x-www-form-urlencoded"
            )

            expires = data["expires_in"]
            self.http.token = data["access_token"]

    ### Alternate constructors

    @classmethod
    async def from_code(cls, client, code, *, redirect_uri, refresh=False):
        route = ("POST", "https://accounts.spotify.com/api/token")
        payload = {
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code": code,
        }

        raw = await client.http.request(
            route, content_type="application/x-www-form-urlencoded", params=payload
        )

        token = raw["access_token"]

        if refresh:
            refresh = (raw["expires_in"], raw["refresh_token"])
        else:
            refresh = None

        return await cls.from_token(client, token, refresh=refresh)

    @classmethod
    async def from_token(cls, client, token, *, refresh=None):
        http = HTTPUserClient(token)
        data = await http.current_user()

        self = cls(client, data=data, http=http, token=token)

        if refresh is not None:
            expires_in, refresh_token, = refresh
            self._refresh_task = self.client.loop.create_task(
                self._refreshing_token(expires_in, refresh_token)
            )

        return self

    ### Attributes

    @property
    def refresh(self):
        return self._refresh_task

    ### Contextual methods

    @ensure_http
    async def currently_playing(self) -> Dict[str, Union[Track, Context, str]]:
        """Get the users currently playing track.

        Returns
        -------
        context, track : Dict[str, Union[Track, Context, str]]
            A tuple of the context and track.
        """
        data = await self.http.currently_playing()

        if 'item' in data:
            context = data.pop('context', None)

            if context is not None:
                data['context'] = Context(context)
            else:
                data['context'] = None

            data["item"] = Track(self.__client, data.get("item"))

        return data

    @ensure_http
    async def get_player(self) -> Player:
        """Get information about the users current playback.

        Returns
        -------
        player : Player
            A player object representing the current playback.
        """
        self._player = player = Player(
            self.__client, self, await self.http.current_player()
        )
        return player

    @ensure_http
    async def get_devices(self) -> List[Device]:
        """Get information about the users avaliable devices.

        Returns
        -------
        devices : List[Device]
            The devices the user has available.
        """
        data = await self.http.available_devices()
        return [Device(item) for item in data["devices"]]

    @ensure_http
    async def recently_played(self) -> List[Dict[str, Union[Track, Context, str]]]:
        """Get tracks from the current users recently played tracks.

        Returns
        -------
        playlist_history : List[Dict[:class:`str`, Union[Track, Context, :class:`str`]]]
            A list of playlist history object.
            Each object is a dict with a timestamp, track and context field.
        """
        data = await self.http.recently_played()
        client = self.__client

        # List[T] where T: {'track': Track, 'content': Context: 'timestamp': ISO8601}
        return [
            {
                "timestamp": track["timestamp"],
                "context": Context(track.get("context")),
                "track": Track(client, track.get("track")),
            }
            for track in data["items"]
        ]

    ### Playlist track methods

    @ensure_http
    async def add_tracks(self, playlist: Union[str, Playlist], *tracks) -> str:
        """Add one or more tracks to a user’s playlist.

        Parameters
        ----------
        playlist : Union[:class:`str`, Playlist]
            The playlist to modify
        tracks : Sequence[Union[:class:`str`, Track]]
            Tracks to add to the playlist

        Returns
        -------
        snapshot_id : :class:`str`
            The snapshot id of the playlist.
        """
        data = await self.http.add_playlist_tracks(
            to_id(str(playlist)), tracks=(str(track) for track in tracks)
        )
        return data["snapshot_id"]

    @ensure_http
    async def replace_tracks(self, playlist, *tracks) -> str:
        """Replace all the tracks in a playlist, overwriting its existing tracks.

        This powerful request can be useful for replacing tracks, re-ordering existing tracks, or clearing the playlist.

        Parameters
        ----------
        playlist : Union[:class:`str`, PLaylist]
            The playlist to modify
        tracks : Sequence[Union[:class:`str`, Track]]
            Tracks to place in the playlist
        """
        await self.http.replace_playlist_tracks(
            to_id(str(playlist)), tracks=",".join(str(track) for track in tracks)
        )

    @ensure_http
    async def remove_tracks(self, playlist, *tracks):
        """Remove one or more tracks from a user’s playlist.

        Parameters
        ----------
        playlist : Union[:class:`str`, Playlist]
            The playlist to modify
        tracks : Sequence[Union[:class:`str`, Track]]
            Tracks to remove from the playlist

        Returns
        -------
        snapshot_id : :class:`str`
            The snapshot id of the playlist.
        """
        data = await self.http.remove_playlist_tracks(
            to_id(str(playlist)), tracks=(str(track) for track in tracks)
        )
        return data["snapshot_id"]

    @ensure_http
    async def reorder_tracks(
        self, playlist, start, insert_before, length=1, *, snapshot_id=None
    ):
        """Reorder a track or a group of tracks in a playlist.

        Parameters
        ----------
        playlist : Union[:class:`str`, Playlist]
            The playlist to modify
        start : int
            The position of the first track to be reordered.
        insert_before : int
            The position where the tracks should be inserted.
        length : Optional[int]
            The amount of tracks to be reordered. Defaults to 1 if not set.
        snapshot_id : :class:`str`
            The playlist’s snapshot ID against which you want to make the changes.

        Returns
        -------
        snapshot_id : :class:`str`
            The snapshot id of the playlist.
        """
        data = await self.http.reorder_playlists_tracks(
            to_id(str(playlist)), start, length, insert_before, snapshot_id=snapshot_id
        )
        return data["snapshot_id"]

    ### Playlist methods

    @ensure_http
    async def edit_playlist(
        self, playlist, *, name=None, public=None, collaborative=None, description=None
    ):
        """Change a playlist’s name and public/private, collaborative state and description.

        Parameters
        ----------
        playlist : Union[:class:`str`, Playlist]
            The playlist to modify
        name : Optional[:class:`str`]
            The new name of the playlist.
        public : Optional[bool]
            The public/private status of the playlist.
            `True` for public, `False` for private.
        collaborative : Optional[bool]
            If `True`, the playlist will become collaborative and other users will be able to modify the playlist.
        description : Optional[:class:`str`]
            The new playlist description
        """
        data = {}

        if name:
            data["name"] = name

        if public:
            data["public"] = public

        if collaborative:
            data["collaborative"] = collaborative

        if description:
            data["description"] = description

        await self.http.change_playlist_details(self.id, to_id(str(playlist)), **data)

    @ensure_http
    async def create_playlist(
        self, name, *, public=True, collaborative=False, description=None
    ):
        """Create a playlist for a Spotify user.

        Parameters
        ----------
        name : :class:`str`
            The name of the playlist.
        public : Optional[bool]
            The public/private status of the playlist.
            `True` for public, `False` for private.
        collaborative : Optional[bool]
            If `True`, the playlist will become collaborative and other users will be able to modify the playlist.
        description : Optional[:class:`str`]
            The playlist description

        Returns
        -------
        playlist : Playlist
            The playlist that was created.
        """
        data = {"name": name, "public": public, "collaborative": collaborative}

        if description:
            data["description"] = description

        playlist_data = await self.http.create_playlist(self.id, **data)
        return Playlist(self.__client, playlist_data, http=self.http)

    async def get_playlists(self, *, limit=20, offset=0):
        """get the users playlists from spotify.

        Parameters
        ----------
         limit : Optional[int]
             The limit on how many playlists to retrieve for this user (default is 20).
         offset : Optional[int]
             The offset from where the api should start from in the playlists.

        Returns
        -------
        playlists : List[Playlist]
            A list of the users playlists.
        """
        if hasattr(self, "http"):
            http = self.http
        else:
            http = self.__client.http

        data = await http.get_playlists(self.id, limit=limit, offset=offset)
        return [
            Playlist(self.__client, playlist_data, http=http)
            for playlist_data in data["items"]
        ]

    @ensure_http
    async def top_artists(self, **data) -> List[Artist]:
        """Get the current user’s top artists based on calculated affinity.

        Parameters
        ----------
        limit : Optional[int]
            The number of entities to return. Default: 20. Minimum: 1. Maximum: 50.
        offset : Optional[int]
            The index of the first entity to return. Default: 0
        time_range : Optional[:class:`str`]
            Over what time frame the affinities are computed. (long_term, short_term, medium_term)

        Returns
        -------
        tracks : List[Artist]
            The top artists for the user.
        """
        return await self._get_top(Artist, data)

    @ensure_http
    async def top_tracks(self, **data) -> List[Track]:
        """Get the current user’s top tracks based on calculated affinity.

        Parameters
        ----------
        limit : Optional[int]
            The number of entities to return. Default: 20. Minimum: 1. Maximum: 50.
        offset : Optional[int]
            The index of the first entity to return. Default: 0
        time_range : Optional[:class:`str`]
            Over what time frame the affinities are computed. (long_term, short_term, medium_term)

        Returns
        -------
        tracks : List[Track]
            The top tracks for the user.
        """
        return await self._get_top(Track, data)
