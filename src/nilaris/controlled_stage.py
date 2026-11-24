"""Guarded graph entry point for native fixtures and labeled literal test work."""
import json
import sys
import time
from typing import TypedDict
from nilaris.worker_lifecycle import arm_worker_deadline


def main():
    identity = arm_worker_deadline()
    # This fixed-size header is stripped and verified against real OS identities
    # before the parent durably records readiness and opens the input gate.
    sys.stdout.write(json.dumps(identity) + '\n')
    sys.stdout.flush()
    # Reading this request gates stage setup on the parent's durable identity.
    request = json.loads(sys.stdin.buffer.read(2 * 1024**2 + 1))
    # Both the launcher and this entry point disable tracing before imports.
    from langchain_core.runnables import RunnableLambda
    from langgraph.graph import END, START, StateGraph
    from nilaris.results import canonical

    class State(TypedDict):
        output: dict
        delay: float
        workload: str
        operation: str
        fixture: dict
        semantic: dict
        capture: dict
        response: dict

    if request.get('operation') == 'semantic172-v1':
        from nilaris.semantic172 import Ledger, digest, check_authorization, execute_capture, finalize_capture, node_event
        import os

        def source_capture(state):
            descriptor = state['semantic']
            sink = Ledger(descriptor['artifact_root'], mode=descriptor['mode'])
            node_event(sink, descriptor, 'source_capture', 'start')
            if digest(sink.verify()) != descriptor['freeze_pin']:
                raise ValueError('freeze mismatch')
            if sink.mode == 'live':
                check_authorization(sink, descriptor['authorization_pin'])
            node_event(sink, descriptor, 'source_capture', 'end')
            return {'capture': sink.read('capture.json')}

        def interpretation(state):
            descriptor = state['semantic']
            sink = Ledger(descriptor['artifact_root'], mode=descriptor['mode'])
            node_event(sink, descriptor, 'model_interpretation', 'start')
            response = execute_capture(sink, descriptor,
                deadline=float(os.environ['NILARIS_CONTROLLED_DEADLINE']))
            node_event(sink, descriptor, 'model_interpretation', 'end')
            return {'response': response}

        def validation(state):
            descriptor = state['semantic']
            sink = Ledger(descriptor['artifact_root'], mode=descriptor['mode'])
            node_event(sink, descriptor, 'validation', 'start')
            result = finalize_capture(sink, descriptor, state['response'])
            node_event(sink, descriptor, 'validation', 'end')
            return {'output': {'semantic_receipt': dict(attempt=descriptor['attempt'], status=result['status'])}}

        graph = StateGraph(State)
        for name, function in (('semantic_source_capture', source_capture),
                ('semantic_model_interpretation', interpretation), ('semantic_validation_receipt', validation)):
            graph.add_node(name, RunnableLambda(function))
        graph.add_edge(START, 'semantic_source_capture')
        graph.add_edge('semantic_source_capture', 'semantic_model_interpretation')
        graph.add_edge('semantic_model_interpretation', 'semantic_validation_receipt')
        graph.add_edge('semantic_validation_receipt', END)
        result = graph.compile().invoke(request)
        sys.stdout.write(canonical(result['output']))
        return

    def controlled(state):
        if state.get('operation') == 'fixture':
            from nilaris.fixture_processing import process
            return {'output': process(state['fixture']).snapshot()}
        if state['workload'] == 'cpu':
            # Fixed finite test work, never a parser or arbitrary executor.
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                pass
        elif state['workload'] == 'rss':
            # Touch pages so this is actual resident memory, not only a virtual
            # reservation. Size and lifetime are fixed controlled-test bounds.
            resident = bytearray(512 * 1024**2)
            for offset in range(0, len(resident), 4096):
                resident[offset] = 1
            time.sleep(20)
        elif state['workload'] == 'output':
            # Fixed overflow probe. A stalled reader applies kernel-pipe
            # backpressure; existing wall/CPU/RSS guards remain independent.
            sys.stdout.write('x' * (2 * 1024**2 + 1))
            sys.stdout.flush()
            time.sleep(20)
        time.sleep(state['delay'])
        return {'output': state['output']}

    graph = StateGraph(State)
    node = ('native_fixture_processing' if request.get('operation') == 'fixture'
            else 'controlled_literal_not_extraction')
    graph.add_node(node, RunnableLambda(controlled))
    graph.add_edge(START, node)
    graph.add_edge(node, END)
    result = graph.compile().invoke(request)
    sys.stdout.write(canonical(result['output']))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # No dependency traceback or request payload escapes this child.
        raise SystemExit(1) from None
