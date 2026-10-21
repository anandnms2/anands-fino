"""Minimal fake of the cTrader Open API JSON endpoint, for tests. Mirrors the documented message shapes."""
import json
import threading
import time

from websockets.sync.server import serve

SYMS = {1: ("EURUSD", 5, 10000000, 100000, 100000, 2, 1), 2: ("XAUUSD", 2, 10000, 100, 100, 3, 1)}  # id: name,digits,lot,min,step,base,quote
PRICE = {1: (1.16500, 1.16512), 2: (4100.10, 4100.40)}


class FakeCTrader:
    def __init__(self, fail_amend=False, reject_order=False):
        self.fail_amend, self.reject_order = fail_amend, reject_order
        self.received: list[dict] = []
        self.positions: dict[int, dict] = {}
        self.deals: list[dict] = []
        self.closed_calls = 0
        self._next_pid = 7000
        self.server = serve(self._handle, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()

    @staticmethod
    def _msg(ws, mid, pt, payload):
        ws.send(json.dumps({"clientMsgId": mid, "payloadType": pt, "payload": payload}))

    def _exec(self, ws, mid, et, position=None, deal=None, order=None):
        p = {"ctidTraderAccountId": 555, "executionType": et}
        if position:
            p["position"] = position
        if deal:
            p["deal"] = deal
        if order:
            p["order"] = order
        self._msg(ws, mid, 2126, p)

    def _handle(self, ws):
        for raw in ws:
            m = json.loads(raw)
            pt, p, mid = m["payloadType"], m.get("payload") or {}, m.get("clientMsgId")
            if pt == 51:
                continue
            self.received.append(m)
            if pt == 2100:
                self._msg(ws, mid, 2101, {})
            elif pt == 2149:
                self._msg(ws, mid, 2150, {"ctidTraderAccount": [{"ctidTraderAccountId": 999, "isLive": True, "traderLogin": 1111},
                                                               {"ctidTraderAccountId": 555, "isLive": False, "traderLogin": 9001, "brokerTitleShort": "FxPro"}]})
            elif pt == 2102:
                self._msg(ws, mid, 2103, {"ctidTraderAccountId": p["ctidTraderAccountId"]})
            elif pt == 2112:
                self._msg(ws, mid, 2113, {"asset": [{"assetId": 1, "name": "USD"}, {"assetId": 2, "name": "EUR"}, {"assetId": 3, "name": "XAU"}]})
            elif pt == 2114:
                self._msg(ws, mid, 2115, {"symbol": [{"symbolId": i, "symbolName": v[0], "baseAssetId": v[5], "quoteAssetId": v[6]} for i, v in SYMS.items()]})
            elif pt == 2116:
                out = []
                for i in p["symbolId"]:
                    n, dg, lot, mn, st, _b, _q = SYMS[i]
                    out.append({"symbolId": i, "digits": dg, "pipPosition": dg - 1, "lotSize": lot, "minVolume": mn, "maxVolume": mn * 100000, "stepVolume": st, "tradingMode": "ENABLED"})
                self._msg(ws, mid, 2117, {"symbol": out})
            elif pt == 2121:
                self._msg(ws, mid, 2122, {"trader": {"ctidTraderAccountId": 555, "balance": 2000000, "moneyDigits": 2, "depositAssetId": 1,
                                                     "leverageInCents": 10000, "traderLogin": 9001, "brokerName": "FxPro"}})
            elif pt == 2127:
                self._msg(ws, mid, 2128, {})
                for i in p["symbolId"]:
                    b, a = PRICE[i]
                    self._msg(ws, None, 2131, {"symbolId": i, "bid": int(b * 1e5), "ask": int(a * 1e5)})
            elif pt == 2137:
                now_min = int(time.time() // 3600) * 60
                n = max(1, min(400, (int(p["toTimestamp"]) - int(p["fromTimestamp"])) // 3600000))
                gold = p["symbolId"] == 2
                bars = []
                for k in range(n):
                    px = (4100.0 if gold else 1.16) * (1 + 0.00004 * k + 0.0006 * ((k * 7) % 5))
                    mul = 100 if gold else 1
                    bars.append({"volume": 10, "low": int(px * 1e5), "deltaOpen": 20 * mul, "deltaClose": 40 * mul, "deltaHigh": 80 * mul,
                                 "utcTimestampInMinutes": now_min - (n - 1 - k) * 60})
                self._msg(ws, mid, 2138, {"period": 9, "symbolId": p["symbolId"], "trendbar": bars, "hasMore": False})
            elif pt == 2124:
                self._msg(ws, mid, 2125, {"position": [x for x in self.positions.values() if x["positionStatus"] == 1]})
            elif pt == 2187:
                self._msg(ws, mid, 2188, {"moneyDigits": 2, "positionUnrealizedPnL": [
                    {"positionId": pid, "grossUnrealizedPnL": 1500, "netUnrealizedPnL": 1250} for pid, x in self.positions.items() if x["positionStatus"] == 1]})
            elif pt == 2106:
                if self.reject_order:
                    self._msg(ws, mid, 2132, {"errorCode": "NOT_ENOUGH_MONEY", "description": "no margin"})
                    continue
                self._exec(ws, mid, 2, order={"orderId": 1, "clientOrderId": p["clientOrderId"]})
                self._next_pid += 1
                pid = self._next_pid
                pos = {"positionId": pid, "positionStatus": 1, "price": PRICE[p["symbolId"]][1 if p["tradeSide"] == 1 else 0], "swap": 0,
                       "tradeData": {"symbolId": p["symbolId"], "volume": p["volume"], "tradeSide": p["tradeSide"], "openTimestamp": int(time.time() * 1000), "label": p["label"]}}
                self.positions[pid] = pos
                self._exec(ws, mid, 3, position=pos, deal={"dealId": 11, "executionPrice": pos["price"]}, order={"orderId": 1, "clientOrderId": p["clientOrderId"]})
            elif pt == 2110:
                if self.fail_amend:
                    self._msg(ws, mid, 2132, {"errorCode": "INVALID_STOPS", "description": "too close", "positionId": p["positionId"]})
                    continue
                pos = self.positions[p["positionId"]]
                pos["stopLoss"], pos["takeProfit"] = p["stopLoss"], p["takeProfit"]
                self._exec(ws, mid, 4, position=pos)
            elif pt == 2111:
                self.closed_calls += 1
                pos = self.positions[p["positionId"]]
                pos["positionStatus"] = 2
                deal = {"dealId": 12, "positionId": p["positionId"], "executionPrice": 1.1660, "symbolId": pos["tradeData"]["symbolId"],
                        "filledVolume": p["volume"], "tradeSide": 2, "executionTimestamp": int(time.time() * 1000), "commission": -350,
                        "moneyDigits": 2, "closePositionDetail": {"entryPrice": pos["price"], "grossProfit": 2500, "swap": -10, "commission": -350, "balance": 2002140, "moneyDigits": 2}}
                self.deals = [{"dealId": 11, "positionId": p["positionId"], "executionPrice": pos["price"], "symbolId": pos["tradeData"]["symbolId"],
                               "filledVolume": p["volume"], "tradeSide": 1, "executionTimestamp": int(time.time() * 1000) - 5000, "commission": -350, "moneyDigits": 2}, deal]
                self._exec(ws, mid, 3, position=pos, deal=deal)
            elif pt == 2133:
                self._msg(ws, mid, 2134, {"deal": self.deals, "hasMore": False})
            else:
                self._msg(ws, mid, 2142, {"errorCode": "UNKNOWN", "description": f"unhandled {pt}"})
