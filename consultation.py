"""Execute one request, preserving partial trace on failure for the UI."""
from langchain_core.messages import HumanMessage

import agent
from reliability import context_signature
from runtime import RequestFailure, classify_error, log_event, request_id


def execute_question(question, history=None, *, offline=False, max_rounds=3, confirmed=None, request=None):
    rid = request or request_id()
    try:
        stamp = context_signature()
        graph = agent.build_graph(model=agent.OfflineDemoModel() if offline else None,
                                  max_rounds=max_rounds, confirmed=confirmed)
        result = graph.invoke({'messages':[*list(history or []),HumanMessage(content=question)],
                               'rounds':0,'trace':[],'request_id':rid,'context_stamp':stamp},
                              {'recursion_limit':2*max_rounds+6})
        log_event(rid,'request_finished')
        return result
    except RequestFailure:
        raise
    except Exception as exc:
        code = classify_error(exc)
        log_event(rid,'request_finished',outcome='failed',code=code)
        raise RequestFailure(code,request=rid) from None
