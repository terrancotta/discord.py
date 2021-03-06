import asyncio
import aiohttp
import websockets
import discord
import inspect

from discord.backoff import ExponentialBackoff

MAX_ASYNCIO_SECONDS = 3456000

class Loop:
    """A background task helper that abstracts the loop and reconnection logic for you.

    The main interface to create this is through :func:`loop`.
    """
    def __init__(self, coro, seconds, hours, minutes, count, reconnect, loop):
        self.coro = coro
        self.seconds = seconds
        self.hours = hours
        self.minutes = minutes
        self.reconnect = reconnect
        self.loop = loop or asyncio.get_event_loop()
        self.count = count
        self._current_loop = 0
        self._task = None
        self._injected = None
        self._valid_exception = (
            OSError,
            discord.HTTPException,
            discord.GatewayNotFound,
            discord.ConnectionClosed,
            aiohttp.ClientError,
            asyncio.TimeoutError,
            websockets.InvalidHandshake,
            websockets.WebSocketProtocolError,
        )

        self._before_loop = None
        self._after_loop = None

        if self.count is not None and self.count <= 0:
            raise ValueError('count must be greater than 0 or None.')

        self._sleep = sleep = self.seconds + (self.minutes * 60.0) + (self.hours * 3600.0)
        if sleep >= MAX_ASYNCIO_SECONDS:
            fmt = 'Total number of seconds exceeds asyncio imposed limit of {0} seconds.'
            raise ValueError(fmt.format(MAX_ASYNCIO_SECONDS))

        if sleep < 0:
            raise ValueError('Total number of seconds cannot be less than zero.')

        if not inspect.iscoroutinefunction(self.coro):
            raise TypeError('Expected coroutine function, not {0.__name__!r}.'.format(type(self.coro)))

    async def _call_loop_function(self, name):
        coro = getattr(self, '_' + name)
        if coro is None:
            return

        if inspect.iscoroutinefunction(coro):
            if self._injected is not None:
                await coro(self._injected)
            else:
                await coro()
        else:
            await coro

    async def _loop(self, *args, **kwargs):
        backoff = ExponentialBackoff()
        await self._call_loop_function('before_loop')
        try:
            while True:
                try:
                    await self.coro(*args, **kwargs)
                except asyncio.CancelledError:
                    break
                except self._valid_exception as exc:
                    if not self.reconnect:
                        raise
                    await asyncio.sleep(backoff.delay())
                else:
                    self._current_loop += 1
                    if self._current_loop == self.count:
                        break

                    await asyncio.sleep(self._sleep)
        finally:
            await self._call_loop_function('after_loop')

    def __get__(self, obj, objtype):
        if obj is None:
            return self
        self._injected = obj
        return self

    @property
    def current_loop(self):
        """:class:`int`: The current iteration of the loop."""
        return self._current_loop

    def start(self, *args, **kwargs):
        r"""Starts the internal task in the event loop.

        Parameters
        ------------
        \*args
            The arguments to to use.
        \*\*kwargs
            The keyword arguments to use.

        Raises
        --------
        RuntimeError
            A task has already been launched.

        Returns
        ---------
        :class:`asyncio.Task`
            The task that has been created.
        """

        if self._task is not None:
            raise RuntimeError('Task is already launched.')

        if self._injected is not None:
            args = (self._injected, *args)

        self._task = self.loop.create_task(self._loop(*args, **kwargs))
        return self._task

    def cancel(self):
        """Cancels the internal task, if any are running."""
        if self._task:
            self._task.cancel()
            self._task = None

    def add_exception_type(self, exc):
        r"""Adds an exception type to be handled during the reconnect logic.

        By default the exception types handled are those handled by
        :meth:`discord.Client.connect`\, which includes a lot of internet disconnection
        errors.

        This function is useful if you're interacting with a 3rd party library that
        raises its own set of exceptions.

        Parameters
        ------------
        exc: Type[:class:`BaseException`]
            The exception class to handle.

        Raises
        --------
        TypeError
            The exception passed is either not a class or not inherited from :class:`BaseException`.
        """

        if not inspect.isclass(exc):
            raise TypeError('{0!r} must be a class.'.format(exc))
        if not issubclass(exc, BaseException):
            raise TypeError('{0!r} must inherit from BaseException.'.format(exc))

        self._valid_exception = (*self._valid_exception, exc)

    def clear_exception_types(self):
        """Removes all exception types that are handled.

        .. note::

            This operation obviously cannot be undone!
        """
        self._valid_exception = tuple()

    def remove_exception_type(self, exc):
        """Removes an exception type from being handled during the reconnect logic.

        Parameters
        ------------
        exc: Type[:class:`BaseException`]
            The exception class to handle.

        Returns
        ---------
        :class:`bool`
            Whether it was successfully removed.
        """
        old_length = len(self._valid_exception)
        self._valid_exception = tuple(x for x in self._valid_exception if x is not exc)
        return len(self._valid_exception) != old_length

    def get_task(self):
        """Optional[:class:`asyncio.Task`]: Fetches the internal task or ``None`` if there isn't one running."""
        return self._task

    def before_loop(self, coro):
        """A function that also acts as a decorator to register a coroutine to be
        called before the loop starts running. This is useful if you want to wait
        for some bot state before the loop starts,
        such as :meth:`discord.Client.wait_until_ready`.

        Parameters
        ------------
        coro: :term:`py:awaitable`
            The coroutine to register before the loop runs.

        Raises
        -------
        TypeError
            The function was not a coroutine.
        """

        if not (inspect.iscoroutinefunction(coro) or inspect.isawaitable(coro)):
            raise TypeError('Expected coroutine or awaitable, received {0.__name__!r}.'.format(type(coro)))

        self._before_loop = coro


    def after_loop(self, coro):
        """A function that also acts as a decorator to register a coroutine to be
        called after the loop finished running.

        Parameters
        ------------
        coro: :term:`py:awaitable`
            The coroutine to register after the loop finishes.

        Raises
        -------
        TypeError
            The function was not a coroutine.
        """

        if not (inspect.iscoroutinefunction(coro) or inspect.isawaitable(coro)):
            raise TypeError('Expected coroutine or awaitable, received {0.__name__!r}.'.format(type(coro)))

        self._after_loop = coro

def loop(*, seconds=0, minutes=0, hours=0, count=None, reconnect=True, loop=None):
    """A decorator that schedules a task in the background for you with
    optional reconnect logic.

    Parameters
    ------------
    seconds: :class:`float`
        The number of seconds between every iteration.
    minutes: :class:`float`
        The number of minutes between every iteration.
    hours: :class:`float`
        The number of hours between every iteration.
    count: Optional[:class:`int`]
        The number of loops to do, ``None`` if it should be an
        infinite loop.
    reconnect: :class:`bool`
        Whether to handle errors and restart the task
        using an exponential back-off algorithm similar to the
        one used in :meth:`discord.Client.connect`.
    loop: :class:`asyncio.AbstractEventLoop`
        The loop to use to register the task, if not given
        defaults to :func:`asyncio.get_event_loop`.

    Raises
    --------
    ValueError
        An invalid value was given.
    TypeError
        The function was not a coroutine.

    Returns
    ---------
    :class:`Loop`
        The loop helper that handles the background task.
    """
    def decorator(func):
        return Loop(func, seconds=seconds, minutes=minutes, hours=hours,
                          count=count, reconnect=reconnect, loop=loop)
    return decorator
