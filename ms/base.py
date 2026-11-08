import asyncio
import websockets

from .protocol_pb2 import Wrapper


class MSRPCChannel:

    def __init__(self, endpoint):
        self._endpoint = endpoint
        self._req_events = {}
        self._new_req_idx = 1
        self._res = {}
        self._hooks = {}

        self._ws = None
        self._msg_dispatcher = None
        # 接收循环一旦退出就置位：否则连接已死，上层却仍会持续发 RPC 并静默超时
        self.closed = False
        self.close_reason = ""

    @property
    def alive(self) -> bool:
        return self._ws is not None and not self.closed

    def add_hook(self, msg_type, hook):
        if msg_type not in self._hooks:
            self._hooks[msg_type] = []
        self._hooks[msg_type].append(hook)

    def unwrap(self, wrapped):
        wrapper = Wrapper()
        wrapper.ParseFromString(wrapped)
        return wrapper

    def wrap(self, name, data):
        wrapper = Wrapper()
        wrapper.name = name
        wrapper.data = data
        return wrapper.SerializeToString()

    async def connect(self, ms_host):
        self._ws = await websockets.connect(self._endpoint, origin=ms_host)
        self._msg_dispatcher = asyncio.create_task(self.dispatch_msg())

    async def close(self):
        self.closed = True
        if self._msg_dispatcher is not None:
            self._msg_dispatcher.cancel()
            try:
                await self._msg_dispatcher
            except asyncio.CancelledError:
                pass
        if self._ws is not None:
            await self._ws.close()

    async def dispatch_msg(self):
        try:
            while True:
                msg = await self._ws.recv()
                type_byte = msg[0]
                if type_byte == 1:  # NOTIFY
                    wrapper = self.unwrap(msg[1:])
                    for hook in self._hooks.get(wrapper.name, []):
                        asyncio.create_task(hook(wrapper.data))
                elif type_byte == 2:  # REQUEST
                    wrapper = self.unwrap(msg[3:])
                    for hook in self._hooks.get(wrapper.name, []):
                        asyncio.create_task(hook(wrapper.data))
                elif type_byte == 3:  # RESPONSE
                    idx = int.from_bytes(msg[1:3], 'little')
                    if not idx in self._req_events:
                        continue
                    self._res[idx] = msg
                    self._req_events[idx].set()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.closed = True
            self.close_reason = f"{type(e).__name__}: {e}"

    async def send_request(self, name, msg, timeout: float = 15.0):
        if self.closed:
            raise ConnectionError(f"channel closed ({self.close_reason})")
        idx = self._new_req_idx
        self._new_req_idx = (self._new_req_idx + 1) % 60007

        wrapped = self.wrap(name, msg)
        pkt = b'\x02' + idx.to_bytes(2, 'little') + wrapped

        evt = asyncio.Event()
        self._req_events[idx] = evt

        try:
            await self._ws.send(pkt)
            await asyncio.wait_for(evt.wait(), timeout)
        except asyncio.TimeoutError:
            # 超时：清理挂起的请求，避免轮询线程永久卡死
            self._req_events.pop(idx, None)
            self._res.pop(idx, None)
            raise TimeoutError(f"RPC timeout: {name}")
        except Exception as e:
            self._req_events.pop(idx, None)
            self.closed = True
            self.close_reason = f"{type(e).__name__}: {e}"
            raise ConnectionError(f"RPC send failed: {name}: {e}") from e

        if not idx in self._res:
            self._req_events.pop(idx, None)
            raise ConnectionError(f"no response for {name}")
        res = self._res.pop(idx)

        if idx in self._req_events:
            del self._req_events[idx]

        body = self.unwrap(res[3:])

        return body.data


class MSRPCService:

    def __init__(self, channel):
        self._channel = channel

    def get_package_name(self):
        raise NotImplementedError

    def get_service_name(self):
        raise NotImplementedError

    def get_req_class(self, method):
        raise NotImplementedError

    def get_res_class(self, method):
        raise NotImplementedError

    async def call_method(self, method, req):
        msg = req.SerializeToString()
        name = '.{}.{}.{}'.format(self.get_package_name(), self.get_service_name(), method)
        res_msg = await self._channel.send_request(name, msg)
        res_class = self.get_res_class(method)
        res = res_class()
        res.ParseFromString(res_msg)
        return res
