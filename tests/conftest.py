"""tests 全局夹具。

STAGE_ZIPPER=0: 关闭 @traced 的拉链双写(gate 见 tracing._zipper_on)——
test_tracing 等纯单测带 run 上下文直调 traced 节点时, 不真实连 PG 落库
(否则污染 stage_chain + PG 不可用时每用例耗 5s 连接超时)。
需要拉链的用例(test_stage_chain)经本地 autouse 夹具重开。
"""
import os

os.environ["STAGE_ZIPPER"] = "0"
