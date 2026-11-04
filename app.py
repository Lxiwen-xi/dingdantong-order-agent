import json

import streamlit as st
from agent import trim_history
from config import read_settings
from confirmation_ui import render_confirmation
from consultation import execute_question
from database import DEMO_DATE, init_db, list_orders
from management_ui import order_table, render_management
from reliability import context_signature, refresh_context
from runtime import RequestFailure, classify_error


def reset_conversation():
    st.session_state.update(turns=[], model_history=[], pending_task=None, pending_confirmation=None)


def retain_failure(question, confirmed, stamp, failure):
    st.session_state['pending_task'] = {'question':question,'confirmed':confirmed,'context_stamp':stamp,
        'request_id':failure.request_id,'code':failure.code,'message':failure.public_message,
        'retryable':failure.retryable,'trace':failure.trace}

st.set_page_config(page_title='订单通 · 智能售后助手', page_icon='📦', layout='wide')
init_db()
st.markdown('''<style>
    .stApp {background:#f7f9fc;} h1,h2,h3{color:#1d3654;}
    [data-testid="stSidebar"]{background:#eef3fa;}
    .block-container{max-width:1160px;padding:2rem 1.5rem;}
    h1{font-size:clamp(1.6rem,3vw,2.5rem)!important;line-height:1.3!important;}
    </style>''', unsafe_allow_html=True)
st.title('订单通 · 智能售后助手')
st.caption('查询订单 · 核对规则 · 退款试算')
st.info('可查询内置演示订单，也可在“订单管理”中添加或导入自己的记录。售后规则为项目演示规则，助手仅查询和试算。')
settings = read_settings()

with st.sidebar:
    st.header('工作台')
    page = st.radio('工作区', ['售后咨询', '订单管理'], key='workspace_page')
    if page == '订单管理':
        st.caption('新增、修改与 CSV 导入均只保存到本机。咨询工具没有修改订单的权限。')
if page == '订单管理':
    render_management()
    st.stop()

with st.sidebar:
    mode_options = ['离线流程演示', 'DeepSeek 智能咨询']
    mode = st.radio('运行模式', mode_options, index=mode_options.index(st.session_state.get('chosen_mode',mode_options[0])),key='run_mode_widget')
    st.session_state['chosen_mode'] = mode
    online = mode == 'DeepSeek 智能咨询'
    if st.session_state.get('previous_mode') != mode:
        reset_conversation()
        st.session_state['previous_mode'] = mode
    rounds = st.slider('最多工具调用轮数', 1, 5, st.session_state.get('chosen_rounds',3), disabled=not online,key='round_limit_widget')
    st.session_state['chosen_rounds'] = rounds
    consent = st.checkbox('允许发送本次问题及相关订单数据到 DeepSeek',value=st.session_state.get('model_consent',False),disabled=not online,key='model_consent_widget')
    st.session_state['model_consent'] = consent
    if settings.key_error:
        st.caption('密钥：本机加密存储未就绪；离线演示仍可使用')
    elif settings.api_key:
        st.caption('密钥：本机加密保存（Windows 当前用户）' if settings.key_source == 'windows_dpapi' else '密钥：已配置（明文来源，建议加密保存）')
    else:
        st.caption('密钥：未配置，可先运行离线演示')
    if online:
        st.caption(f'模型：{settings.model}')
        st.caption('密钥请运行 设置DeepSeek密钥.py 配置；.env 仅填写模型与接口地址。保存后重新提交即可读取。')
    if st.button('开始新会话', width='stretch'):
        reset_conversation()
        st.rerun()
    st.divider()
    st.caption('保留最近5轮完整上下文。刷新或关闭服务后不提供长期会话保存。')

st.session_state.setdefault('turns', [])
st.session_state.setdefault('model_history', [])
try:
    stamp = context_signature()
except Exception:
    st.error('订单或规则文件暂时无法读取，请检查本地数据后刷新。')
    st.stop()
previous_stamp = st.session_state.get('context_stamp')
if previous_stamp and previous_stamp != stamp:
    st.session_state['model_history'] = refresh_context(st.session_state['model_history'])
    st.session_state['pending_confirmation'] = None
    label = '订单数据已更新' if previous_stamp.get('orders') != stamp['orders'] else '业务日期或售后规则已变化'
    st.info(label + '，后续咨询会重新查询。用户原话保留，下方已有回答仅作为历史记录。')
elif previous_stamp is None and st.session_state['model_history']:
    st.session_state['model_history'] = refresh_context(st.session_state['model_history'])
st.session_state['context_stamp'] = stamp
question = None
confirmed = None
if not online:
    st.caption('离线演示固定执行：查询 A1001 → 查询数码规则 → 试算退 1 副未拆封耳机。三个工具均真实执行，不调用模型。')
    if st.button('运行离线演示', type='primary'):
        question = 'A1001 的耳机还没拆封，想退1副，预计能退多少钱？'

with st.expander('查看当前订单与试用问题', expanded=not st.session_state['turns']):
    st.caption(f'内置演示订单按 {DEMO_DATE} 计算；本地录入订单按当前北京时间日期计算。')
    st.dataframe(order_table(list_orders()), hide_index=True, width='stretch')
    st.markdown('可以问：\n- A1001 订单到哪了？\n- 我想退 A1001 的一副耳机，还没拆封。\n- A1002 的音箱能无理由退吗？\n- A1006 的外套吊牌还在，也没有穿过，退一件能退多少钱？\n- 那换货呢？')


def display_turn(turn):
    with st.chat_message('user'):
        st.write(turn['question'])
    with st.chat_message('assistant'):
        st.markdown(turn['answer'])
        if turn.get('answer_check',{}).get('status') == 'grounded_fallback':
            st.caption('本次按工具结果给出答复。')
        if turn.get('request_id'):
            st.caption('请求编号：' + turn['request_id'])
        if turn['trace']:
            with st.expander(f"查看本次工具记录（{len(turn['trace'])}次）"):
                for item in turn['trace']:
                    st.markdown(f"**{item['tool']} · {'执行完成' if item['success'] else '需核对'}**")
                    st.json(item)


for turn in st.session_state['turns']:
    display_turn(turn)

pending = st.session_state.get('pending_task')
if pending:
    with st.chat_message('user'):
        st.write(pending['question'])
    with st.chat_message('assistant'):
        if pending['code']=='consent_required':
            st.warning(pending['message'])
        else:
            st.error(pending['message'])
        st.caption('原问题已保留。请求编号：' + pending['request_id'])
        if pending['trace']:
            with st.expander('查看失败前的工具记录'):
                st.json(pending['trace'])
        if st.button('重试本次问题',type='primary',key='retry_question'):
            question = pending['question']
            confirmed = pending.get('confirmed') if pending.get('context_stamp') == stamp else None

if online and st.session_state.get('pending_confirmation'):
    confirmation = render_confirmation(st.session_state['pending_confirmation'])
    if confirmation:
        question, confirmed = confirmation

if online:
    st.caption('模型根据问题决定是否查询或追问，工具提供订单事实、规则和金额。请勿输入真实隐私信息。')
    chat_question = st.chat_input('描述你想查询的订单或售后问题…', max_chars=2000)
    if chat_question:
        question, confirmed = chat_question, None

if question:
    if online and not consent:
        retain_failure(question,confirmed,stamp,RequestFailure('consent_required'))
        st.rerun()
    elif online and not settings.api_key:
        retain_failure(question,confirmed,stamp,RequestFailure(settings.key_error or 'configuration'))
        st.rerun()
    else:
        try:
            history = trim_history(st.session_state['model_history']) if online else []
            with st.spinner('正在查询与整理…'):
                result = execute_question(question, history, offline=not online,
                                          max_rounds=rounds if online else 3, confirmed=confirmed)
            turn = {'question': question, 'answer': str(result['messages'][-1].content), 'trace': result['trace'],
                    'request_id':result['request_id'], 'answer_check':result.get('answer_check',{})}
            st.session_state['turns'].append(turn)
            st.session_state['turns'] = st.session_state['turns'][-10:]
            st.session_state['model_history'] = trim_history(result['messages']) if online else []
            st.session_state['pending_task'] = None
            st.session_state['pending_confirmation'] = (result.get('confirmations') or [None])[-1]
            st.rerun()
        except Exception as exc:
            failure = exc if isinstance(exc,RequestFailure) else RequestFailure(classify_error(exc))
            retain_failure(question,confirmed,stamp,failure)
            st.rerun()

if st.session_state['turns']:
    st.download_button('导出当前会话与工具记录', json.dumps(st.session_state['turns'], ensure_ascii=False, indent=2),
                       file_name='order-agent-session.json', mime='application/json')
