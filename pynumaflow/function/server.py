import asyncio
import logging
from os import environ
from typing import Callable, Iterator

import grpc
from google.protobuf import empty_pb2 as _empty_pb2

from pynumaflow._constants import (
    FUNCTION_SOCK_PATH,
    DATUM_KEY,
    MAX_MESSAGE_SIZE,
)
from pynumaflow.function import Messages, Datum
from pynumaflow.function.generated import udfunction_pb2
from pynumaflow.function.generated import udfunction_pb2_grpc
from pynumaflow.types import NumaflowServicerContext

if environ.get("PYTHONDEBUG"):
    logging.basicConfig(level=logging.DEBUG)

_LOGGER = logging.getLogger(__name__)

UDFMapCallable = Callable[[str, Datum], Messages]


class UserDefinedFunctionServicer(udfunction_pb2_grpc.UserDefinedFunctionServicer):
    """
    Provides an interface to write a User Defined Function (UDFunction)
    which will be exposed over gRPC.

    Args:
        map_handler: Function callable following the type signature of UDFMapCallable
        sock_path: Path to the UNIX Domain Socket
        max_message_size: The max message size in bytes the server can receive and send

    Example invocation:
    >>> from pynumaflow.function import Messages, Message, Datum, UserDefinedFunctionServicer
    >>> def map_handler(key: str, datum: Datum) -> Messages:
    ...   val = datum.value
    ...   _ = datum.event_time
    ...   _ = datum.watermark
    ...   messages = Messages()
    ...   messages.append(Message.to_vtx(key, val))
    ...   return messages
    >>> grpc_server = UserDefinedFunctionServicer(map_handler)
    >>> grpc_server.start()
    """

    def __init__(
        self,
        map_handler: UDFMapCallable,
        sock_path=FUNCTION_SOCK_PATH,
        max_message_size=MAX_MESSAGE_SIZE,
    ):
        self.__map_handler: UDFMapCallable = map_handler
        self.sock_path = f"unix://{sock_path}"
        self._max_message_size = max_message_size
        self._cleanup_coroutines = []

    def MapFn(
        self, request: udfunction_pb2.Datum, context: NumaflowServicerContext
    ) -> udfunction_pb2.DatumList:
        """
        Applies a function to each datum element.
        The pascal case function name comes from the generated udfunction_pb2_grpc.py file.
        """
        key = ""
        for metadata_key, metadata_value in context.invocation_metadata():
            if metadata_key == DATUM_KEY:
                key = metadata_value

        try:
            msgs = self.__map_handler(
                key,
                Datum(
                    value=request.value,
                    event_time=request.event_time.event_time.ToDatetime(),
                    watermark=request.watermark.watermark.ToDatetime(),
                ),
            )
        except Exception as err:
            _LOGGER.critical("UDFError, dropping message on the floor: %r", err, exc_info=True)

            # a neat hack to drop
            msgs = Messages.as_forward_all(None)

        datums = []
        for msg in msgs.items():
            datums.append(udfunction_pb2.Datum(key=msg.key, value=msg.value))

        return udfunction_pb2.DatumList(elements=datums)

    def ReduceFn(
        self, request_iterator: Iterator[Datum], context: NumaflowServicerContext
    ) -> udfunction_pb2.DatumList:
        """
        Applies a reduce function to a datum stream.
        The pascal case function name comes from the generated udfunction_pb2_grpc.py file.
        """
        # TODO: implement Reduce function
        context.set_code(grpc.StatusCode.UNIMPLEMENTED)
        context.set_details("Method not implemented!")
        raise NotImplementedError("Method not implemented!")

    def IsReady(
        self, request: _empty_pb2.Empty, context: NumaflowServicerContext
    ) -> udfunction_pb2.ReadyResponse:
        """
        IsReady is the heartbeat endpoint for gRPC.
        The pascal case function name comes from the generated udfunction_pb2_grpc.py file.
        """
        return udfunction_pb2.ReadyResponse(ready=True)

    async def __serve(self) -> None:
        server = grpc.aio.server(
            options=[
                ("grpc.max_send_message_length", self._max_message_size),
                ("grpc.max_receive_message_length", self._max_message_size),
            ]
        )
        udfunction_pb2_grpc.add_UserDefinedFunctionServicer_to_server(
            UserDefinedFunctionServicer(self.__map_handler), server
        )
        server.add_insecure_port(self.sock_path)
        _LOGGER.info("Server listening on: %s", self.sock_path)
        await server.start()

        async def server_graceful_shutdown():
            logging.info("Starting graceful shutdown...")
            # Shuts down the server with 5 seconds of grace period. During the
            # grace period, the server won't accept new connections and allow
            # existing RPCs to continue within the grace period.
            await server.stop(5)

        self._cleanup_coroutines.append(server_graceful_shutdown())
        await server.wait_for_termination()

    def start(self) -> None:
        """Starts the server on the given UNIX socket."""
        loop = asyncio.get_event_loop()
        try:
            loop.run_until_complete(self.__serve())
        finally:
            loop.run_until_complete(*self._cleanup_coroutines)
            loop.close()
