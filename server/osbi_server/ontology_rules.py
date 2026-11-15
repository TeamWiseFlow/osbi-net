"""Entity rules adapted from onto_pilot v2.6; no pilot runtime dependency."""
import dataclasses
import re
from typing import Any, Dict, List, Tuple

HINT_SEP = "｜"


ENTITY_HINTS: Dict[str, str] = {
    "人物": "只提人名本身(2-4字)，职务称谓剥离、不要带在人物名里；"
            "企业高管、学者、记者、评论员用人物，党政机关领导用领导干部",
    "领导干部": "只提人名本身，不带职务前缀后缀(书记/区长/主任等)；"
              "仅用于党政机关/政府机构任职或有完整政府职务的人名",
    "职务": "完整职务称谓，必须带具体行政区划或机构名(如'克州党委书记'"
            "'上海市数据局局长''某银行首席经济学家')；裸书记/主任/首席经济学家丢弃；"
            "只写'省委/市委/区委'而没有具体省市区名的职务丢弃，与人物分开提取",
    "政府机构（含党组织）": "机构名必须带行政区划/辖区限定(如'克州党委''临汾路街道党工委'，"
                          "不能只提'党委''党工委')；含各级社会工作部(文中'XX社会工作'即其简称)；"
                          "指政府机关时用全称(如'安徽省人民政府')，裸地区名不算机构；"
                          "行政区域名(嘉兴市、秀洲区、洪合镇)按对应级别提取，供'位于'关系使用",
    "社会群体": "带限定语的具体群体(新就业群体、老年志愿者、外卖骑手、个体工商户)；"
              "'群众''居民''市民''社区居民''志愿者''商户'等无限定泛称不要提取",
    "概念议题": "治理议题与宏观理念(基层治理现代化、共同富裕、营商环境)；"
             "CPI、GDP等客观名词不要放这里：经济库归'通用名词'，政策库不提取；"
             "基层治理/社区治理/社会治理/基层党建/协同治理/高质量发展等伞形词不要单独成节点",
    "通用名词": "客观名词：统计指标、专业术语(CPI、GDP、数字化转型、可再生能源)；仅经济库使用",
    "治理机制": "有专名的工作法、机制、基层治理品牌——带引号的品牌名也算"
             "(如'她力量''新青年''三所联动''新时代枫桥经验')；"
             "党建引领/志愿服务/联合巡查/联合处置/联合会商/吹哨等泛流程词不要提取",
    "机构组织": "机构名必须带行政区划/辖区限定('宝华里居民区居委会'而非'居委会')；"
              "带引号的治理品牌新名词不是机构，归'治理机制'",
    "境外组织": "境外企业、社会团体等其余境外主体；政策库统一用本类，经济库优先使用"
              "更细的境外国家/地区、境外政府机构、国际组织；港澳台不得归入",
    "境外国家/地区": "主权国家、地区、外国城市/行政区(美国、日本、欧元区、莫斯科)；经济库细分用",
    "境外政府机构": "外国中央政府机构/央行/白宫/美联储等(日本总务省、美国能源部、俄罗斯央行)；经济库细分用",
    "国际组织": "政府间/国际组织(欧盟、联合国、经合组织、世界银行)；经济库细分用",
    "金融机构": "带名称的具体持牌金融机构：银行、基金公司、保险公司、证券公司、资产管理公司；"
              "裸'银行''基金公司'等通名不提取",
    "交易场所": "具体交易场所：上海证券交易所、上海期货交易所、上海黄金交易所、北京证券交易所；"
              "裸'交易所'不提取",
    "金融产品": "可交易或可申购的具体金融产品/指数：ETF、基金、股票、债券、股指、期货合约；"
              "泛称'金融产品'不提取",
    "经济指标": "可量化的宏观经济指标：CPI、GDP、PMI、M2、社会融资规模、失业率、进出口总额",
    "居民活动": "社区居民文体活动及居民文体团队，如彩凤鼓乐队；在本业务中按居民活动提取，不作为机构",
    "会议论坛": "可唯一定位的会议，名称带主办方/地区等限定；裸民营企业座谈会、10月份新闻发布会不提取",
}

PREDICATE_HINTS: Dict[str, str] = {
    "现任": "人物 现任 职务(如'陈吉宁 现任 上海市委书记')",
    "考察": "参观/视察/考察/走访对象统一用本谓词",
    "落实": "行动落实政策的强关联；仅顺带提到政策用'提及'",
    "强调": "领导讲话强调的主题(议题/政策/群体)",
    "上市": "企业或ETF/基金 上市/挂牌于 交易场所",
    "跟踪": "ETF/基金 跟踪 指数、商品或资产标的",
    "发布数据": "机构 发布 经济指标(如'国家统计局 发布数据 CPI')",
    "位于": "机构/场所/企业/平台 位于 行政区域(省/市/区县/街镇)；"
            "指向原文同句出现、最明确的区县级区域，如'洪合毛衫市场 位于 秀洲区'",
    "隶属": "行政区域或机构的上下级：区县 隶属 地市、街镇 隶属 区县；仅原文明确时提取",
}


def strip_hint(label: str) -> str:
    """Remove decorative type hints from a model-returned label."""
    return label.split(HINT_SEP)[0].strip()




PERSON_LABELS = {"人物", "领导干部"}
ORG_LABELS = {
    "机构组织", "政府机构（含党组织）", "国家级机构", "省级机构", "市级机构",
    "区级机构", "街镇机构", "社区自治组织", "社会组织", "企业", "境外组织",

    "金融机构", "交易场所",
    "境外国家/地区", "境外政府机构", "国际组织",
}
BARE_GROUPS = {"群众", "居民", "村民", "市民", "群众们", "党员",
               "社区居民", "志愿者", "商户", "社会力量"}


GENERIC_MECHANISMS = {
    "党建引领", "志愿服务", "联合巡查", "联合处置", "联合会商", "吹哨",
    "吹哨报到", "共建共治共享", "网格化管理", "精细化治理", "多元共治",
    "三治融合", "自治法治德治", "党建引领基层治理", "党建引领社会治理",
    "基层治理", "社会治理", "社区治理", "协同治理", "治理现代化",
    "现代化治理", "智慧治理", "数字治理", "共建共享", "共治共享",
}
GENERIC_CONCEPTS = {
    "基层治理", "社区治理", "社会治理", "基层党建", "协同治理",
    "治理现代化", "共建共治共享", "高质量发展",
}
EVENT_TEXT_LABELS = {
    "会议论坛", "事件", "调研视察", "居民活动", "行动事项", "市级行动",
    "区级行动", "街镇行动", "社区行动",
}


BARE_ORGS = {
    "党委", "党工委", "党组织", "社区党组织", "政府", "管委会", "居委会",
    "业委会", "街道办事处", "办事处", "服务中心", "党群服务中心", "机关",
    "镇政府", "乡政府", "市政府", "区政府", "县政府", "司法所", "公安局", "派出所",
    "银行", "基金公司", "证券公司", "保险公司", "交易所", "交易场所",
}



GENERIC_PHRASES = set("""遍地开花 常驻旅游团队 成长潜力 充分论证 存量政策 打造
典型应用场景 对接活动 多参与 放心投 公共场景 公平开放 管理规范 坚持走 加快壮大
建立 基础条件 技术方案 技术供给方 技术含量 技术路线 民营企业座谈会""".split())
NATIONAL_BODIES = {'公安部', '司法部', '国务院', '中共中央'}

DOMESTIC_COUNTRY_NAMES = {'中国', '中华人民共和国'}
BARE_POSTS = {'书记', '主任', '首席经济学家', '经济学家', '负责人', '专家', '教授', '董事', '委员'}


POLICY_LABELS = {
    '政策文件', '国家级政策', '省级政策', '市级政策', '区级政策',
    '街镇级政策', '政策征求意见稿',
}
REGION_LABELS = {'国家级机构', '省级机构', '市级机构', '区级机构', '街镇机构'}
OVERSEAS_LABELS = {'境外组织', '境外国家/地区', '境外政府机构', '国际组织'}

PUBLIC_INSTITUTION_SUFFIXES = (
    '医院', '中医院', '大学', '学院', '学校', '广播电视台', '电视台', '电台', '总台',
    '日报', '晚报', '报社', '出版社', '研究院', '研究所', '科学院', '图书馆',
    '博物馆', '文化馆', '体育馆', '剧院', '歌舞团', '报',
)

GENERIC_REGION_NAMES = {
    '全国', '我国', '城镇', '乡村', '东部地区', '西部地区', '中部地区', '东北地区',
    '东部', '西部', '中部', '东北', '城区', '郊区', '开发区', '园区', '景区',
    '校区', '厂区', '社区', '小区',
}
DROP_SCOPE_ENTITIES = {
    '全国', '我国', '城镇', '乡村', '东部地区', '西部地区', '中部地区', '东北地区',
}
BARE_MEETINGS = {
    '党委（扩大）会议', '党委扩大会议', '工作会议', '座谈会', '研讨会', '推进会',
    '培训班', '发布会', '新闻发布会', '民营企业座谈会', '专题会议', '联席会议',
    '部署会', '动员会', '表彰大会', '大会', '会议',
}

ORG_CANONICAL = {
    '发改委': '国家发展和改革委员会', '国家发改委': '国家发展和改革委员会',
    '国家发展改革委': '国家发展和改革委员会',
    '住建部': '住房和城乡建设部', '中华人民共和国住房和城乡建设部': '住房和城乡建设部',
    '人社部': '人力资源和社会保障部',
    '卫健委': '国家卫生健康委员会', '国家卫健委': '国家卫生健康委员会',
    '工信部': '工业和信息化部', '科技部': '科学技术部',
    '市场监管总局': '国家市场监督管理总局',
    '税务总局': '国家税务总局', '统计局': '国家统计局',
    '央行': '中国人民银行', '证监会': '中国证券监督管理委员会',
    '金融监管总局': '国家金融监督管理总局',
    '中央网信办': '国家互联网信息办公室', '国家网信办': '国家互联网信息办公室',
}

KNOWN_ENTITY_TYPES = {
    '中国': '国家级机构', '中国人民银行': '国家级机构',
    '新华社': '机构组织', '新华通讯社': '机构组织',
    '人民日报': '机构组织', '经济日报': '机构组织',
    '央视财经': '机构组织', '中央广播电视总台': '机构组织',
}
BOARD_LABELS = {2: '经济动态', 8: '社区治理', 9: '经济动态', 10: '基层党建',
                11: '政策速递', 12: '数字社区'}



TYPE_ONLY_NOUNS = {

    '市场', '资源', '平台', '机制', '体系', '活动', '项目', '产品', '服务',
    '部门', '队伍', '阵地', '举措', '手段', '流程', '样本', '模板', '品牌',
    '名片', '秩序', '答卷', '竞赛', '培训', '课程', '小组', '国库', '商务',
    '体育', '统计', '税务', '海关', '法院', '工会', '妇联', '共青团',
    '管委会', '服务中心', '工作室', '调解室', '警务室', '议事角', '咖啡屋',
    '博物馆', '工人文化宫', '青少年活动中心', '学堂', '之家', '哨兵',
    '最后一公里', '角', '账', '码', '汇', '派', '榜', '党总支', '党支部',
    '行业党委', '流动党员党支部', '流动党员党组织',
    '基层党组织', '街区党组织', '社区党组织', '两新党组织',
    '社会工作部', '社会工作部门', '党委社会工作部', '数据局', '大数据中心',
    '数据管理部门', '数据资源管理局', '发展改革部门', '市场监管部门',
    '市场监督管理局', '市场监管局', '税务部门', '财政部门', '审计机关',
    '公安机关', '司法行政部门', '党群服务中心', '新时代文明实践中心',
    '新时代文明实践站', '认证监管机构', '认证监管司', '住房公积金管理中心',

    '企业', '金融机构', '保险机构', '国企', '央企', '央国企', '国有企业', '中央企业',
    '民营企业', '大型企业', '中小企业', '小微企业', '科技企业',
    '科技型中小企业', '科技型初创企业', '科技领军企业', '高新技术企业',
    '创新型中小企业', '专精特新中小企业', '制造业单项冠军企业',
    '国有金融机构', '非公有制企业', '外资创业投资机构', '国际标准组织',
    '自由贸易试验区', '国有资本占控股地位或者主导地位的企业、金融机构',
}


OVERSEAS_ORGS = {
    '欧盟', '欧盟委员会', '联合国', '联合国开发计划署', '世界银行', '世界银行集团',
    '国际货币基金组织', '世界贸易组织', '世贸组织', '经济合作与发展组织', '经合组织',
    '美联储', '欧洲央行', '日本央行', '日本总务省', '美国商务部', '美国财政部',
    '世界卫生组织', '国际劳工组织', '国际原子能机构', '东盟', '非盟', '北约',
}
_OVERSEAS_PREFIX = re.compile(
    r'^(美国|日本|英国|法国|德国|韩国|俄罗斯|印度|巴西|澳大利亚|加拿大|新加坡|'
    r'欧盟|联合国|世界银行|国际货币基金组织|世界贸易组织|世贸组织|经合组织|'
    r'东盟|非盟|北约|国际原子能机构|世界卫生组织|国际劳工组织)')

OVERSEAS_COUNTRIES = {
    '美国', '日本', '俄罗斯', '德国', '法国', '英国', '意大利', '西班牙', '加拿大',
    '澳大利亚', '土耳其', '巴基斯坦', '埃及', '沙特阿拉伯', '伊朗', '印度', '巴西',
    '韩国', '新加坡', '墨西哥', '阿根廷', '南非', '越南', '泰国', '马来西亚',
    '印度尼西亚', '印尼', '菲律宾', '荷兰', '瑞士', '瑞典', '挪威', '芬兰', '丹麦',
    '波兰', '葡萄牙', '希腊', '奥地利', '比利时', '爱尔兰', '新西兰', '以色列', '乌克兰',
}
OVERSEAS_REGIONS = {
    '欧元区', '欧洲', '北美', '亚太', '东南亚', '中东', '拉美', '非洲',
    '莫斯科', '马德里', '名古屋', '纽约', '伦敦', '东京', '巴黎', '柏林', '首尔',
    '堪察加边疆区', '堪察加彼得罗巴甫洛夫斯克',
}
OVERSEAS_GOVS = {
    '美联储', '欧洲央行', '日本央行', '俄罗斯央行', '日本总务省',
    '美国商务部', '美国财政部', '美国能源部能源信息局', '白宫',
}
INTERNATIONAL_ORGS = {
    '欧盟', '欧盟委员会', '联合国', '联合国开发计划署', '世界银行', '世界银行集团',
    '国际货币基金组织', '世界贸易组织', '世贸组织', '经济合作与发展组织', '经合组织',
    '东盟', '非盟', '北约', '国际原子能机构', '世界卫生组织', '国际劳工组织',
}
INTERNATIONAL_WORDS = ('联合国', '国际', '世界', '经合', '东盟', '非盟', '北约', '欧盟')
AGREEMENTS = {'RCEP', 'CPTPP', 'USMCA'}
_AGREEMENT_SUFFIX = ('关税法', '协定', '协议', '条约')
_GOV_SUFFIX = re.compile(r'(部|局|署|厅|省|委员会|央行|政府|使馆|白宫)$')
_FINANCIAL_FUNCTIONS = {'银行', '证券公司', '基金公司', '保险公司', '资产管理公司', '交易所'}

_DOMESTIC_SAR = re.compile(r'^(澳门|香港|台湾)')
_SAR_GOV_SUFFIX = re.compile(r'(政府|局|署|厅|委员会|办公室|办事处)$')


PROVINCE_CANONICAL = {
    '北京': '北京市', '上海': '上海市', '天津': '天津市', '重庆': '重庆市',
    '河北': '河北省', '山西': '山西省', '辽宁': '辽宁省', '吉林': '吉林省',
    '黑龙江': '黑龙江省', '江苏': '江苏省', '浙江': '浙江省', '安徽': '安徽省',
    '福建': '福建省', '江西': '江西省', '山东': '山东省', '河南': '河南省',
    '湖北': '湖北省', '湖南': '湖南省', '广东': '广东省', '海南': '海南省',
    '四川': '四川省', '贵州': '贵州省', '云南': '云南省', '陕西': '陕西省',
    '甘肃': '甘肃省', '青海': '青海省', '台湾': '台湾省',
    '内蒙古': '内蒙古自治区', '广西': '广西壮族自治区', '西藏': '西藏自治区',
    '宁夏': '宁夏回族自治区', '新疆': '新疆维吾尔自治区',
}
CITY_CANONICAL = {
    '北京': '北京市', '上海': '上海市', '天津': '天津市', '重庆': '重庆市',
    '阿拉善': '阿拉善盟', '大兴安岭': '大兴安岭地区',
    '恩施': '恩施土家族苗族自治州', '湘西': '湘西土家族苗族自治州',
    '香港': '香港', '澳门': '澳门',
}

CITY_NAMES = {
    '嘉兴', '青岛', '宁波', '兰州', '三亚', '儋州', '合肥', '广州', '深圳', '珠海',
    '汕头', '河源', '韶关', '佛山', '长治', '文昌', '乌兰察布', '克拉玛依',
    '巴彦淖尔', '那曲', '杭州', '苏州', '南京', '武汉', '成都', '西安', '郑州',
    '济南', '长沙', '福州', '厦门', '南昌', '昆明', '贵阳', '南宁', '海口', '太原',
    '石家庄', '沈阳', '长春', '哈尔滨', '呼和浩特', '银川', '西宁', '乌鲁木齐',
    '拉萨', '大连', '烟台', '潍坊', '临沂', '洛阳', '襄阳', '宜昌', '株洲', '湘潭',
    '惠州', '东莞', '中山', '江门', '湛江', '茂名', '肇庆', '清远', '揭阳', '潮州',
    '梅州', '汕尾', '阳江', '云浮', '衢州', '绍兴', '湖州', '台州', '舟山', '金华',
    '丽水', '温州',
}
_CITY_SUFFIXES = ('市', '盟', '地区', '自治州', '特别行政区', '区', '县', '旗')


_REGION_LEVEL = {'省': 1, '自治区': 1, '自治州': 1, '市': 2, '区': 3, '县': 3,
                 '镇': 4, '乡': 4, '街道': 4}

_LOCATION_PRIORITY = {'区': 4, '县': 4, '市': 3, '省': 2, '自治区': 2,
                      '自治州': 2, '镇': 1, '乡': 1, '街道': 1}
_AGENCY_SUFFIX = re.compile(r'(局|委|办|部|院|中心|站|所|会|校|馆|队|公司|银行|集团|社|署|厅)$')


def qualify_organization(text, context, *, city=None, district=None):
    """Resolve local abbreviations using explicit original-text jurisdiction only."""
    compact = re.sub(r'\s+', '', context)
    if city is not None and (not city.endswith('市') or city not in compact):
        return ''
    if district is not None and (not district.endswith(('区', '县')) or district not in compact):
        return ''


    if district is None and city:

        districts = set(re.findall(re.escape(city) + r'([一-龥]{1,5}?[区县])', compact))
        if len(districts) == 1:
            district = next(iter(districts))
    local_reference = text.startswith(('区', '县', '市'))
    data_body = bool(re.search(r'(?:数据局|大数据中心|数据管理局|数据资源管理局)$', text))
    office = bool(re.search(r'(?:办|办公室)$', text))
    if not (local_reference or data_body or office):
        return text




    if re.match(r'^(?:国家|中国|全国|中央|国务院)', text) or re.match(r'^[一-龥]{2,}(?:省|自治区)', text):
        return text
    if re.search(r'[一-龥]{2,}市', text) and not local_reference:
        return text
    if city and city in text:
        return text
    if local_reference:
        if not city or (text.startswith(('区', '县')) and (not district or not district.endswith(text[0]))):
            return ''
        return city + ('' if text.startswith('市') else district) + text[1:]
    if data_body:
        if not city:
            return ''

        if re.search(r'[区县]', text):
            if not district or not text.startswith(district):
                return ''
            return city + text
        return city + (district or '') + text
    if office:
        if re.search(r'[一-龥]{2,}(?:市|区|县|镇|街道|社区|村).+(?:办|办公室)$', text):
            return text
        if city:
            return city + (district or '') + text

        full = set(re.findall(r'(?:^|[，。；：\s])([一-龥]{2,16}?(?:区|县|镇|街道|社区)' + re.escape(text) + ')', context))
        return next(iter(full)) if len(full) == 1 else ''
    return text


_POST_PLACE_RE = re.compile(
    r'[一-龥]{2,8}(?:省|市|区|县|镇|街道|自治区|自治州)(?!长)')
_GENERIC_POST_PREFIX_RE = re.compile(
    r'^(?:省委|市委|区委|县委|镇党委|乡党委|自治区党委|州委|政府|党委|党组)')


def _has_specific_place(text):
    for match in _POST_PLACE_RE.finditer(text or ''):
        value = match.group(0)
        if value in ('自治区', '自治州'):
            continue
        if value.endswith(_NON_ADMIN_AREA_SUFFIXES):
            continue
        return True
    return False


def qualified_post(text):
    if text in {'国家主席', '国家副主席', '中共中央总书记', '国务院总理', '国务院副总理'}:
        return True
    if text in BARE_POSTS:
        return False


    if _GENERIC_POST_PREFIX_RE.match(text) and not _has_specific_place(text):
        return False
    match = re.search(rf'(?:副)?(?:{_TITLE_CORE}|首席经济学家|经济学家|负责人|教授|董事|委员)$', text)
    return bool(match and len(text[:match.start()]) >= 2)


def canonical_reference(text, context):
    """Expand only an explicit document alias in this original block/title."""
    if not re.fullmatch(r'《?(?:意见|通知|方案|办法|规定|条例)》?', text):
        return text
    short = text.strip('《》')
    pattern = r'《([^《》\n]{3,160})》[（(](?:以下简称|简称)[“"《]?' + re.escape(short) + r'[”"》]?[）)]'
    matches = set(re.findall(pattern, context))
    return '《' + next(iter(matches)) + '》' if len(matches) == 1 else ''


_POLICY_ISSUER_PREFIX_RE = re.compile(
    r'^(?:中办、国办|中共中央办公厅、国务院办公厅|中共中央办公厅|国务院办公厅|中办|国办)')
_WHITESPACE_RE = re.compile(r'\s+')


def canonical_policy_text(text):
    """Policy display/URI name: drop book marks and issuer wrappers.

    Version markers and internal spacing are preserved; the query-layer view
    merges whitespace-only variants without changing the extracted display.
    """
    value = str(text or '').strip()
    value = _POLICY_ISSUER_PREFIX_RE.sub('', value).strip()
    value = value.strip('《》').strip()
    return value or str(text or '').strip()


def canonical_event_text(text):
    """Normalize whitespace in meeting/event/inspection names."""
    value = str(text or '').strip()
    return _WHITESPACE_RE.sub('', value) or value


INSPECTION_VERB_RE = re.compile(
    r'(赴|到|前往|深入|调研|考察|走访|慰问|检查|督导|接访)')


_POLICY_TITLE_SUFFIX = re.compile(
    r'(?:条例|办法|意见|通知|方案|规定|决定|规划|细则|公告|指引|标准|法律|'
    r'政策措施|行动计划|实施方案)(?:[（(][^）)]*[）)])?$')
_MEETING_SUFFIX = re.compile(
    r'(?:会议|培训班|推进会|座谈会|研讨会|论坛|大会|工作会|部署会|交流会|'
    r'发布会|宣讲会|联席会)(?:[（(][^）)]*[）)])?$')
_MEETING_EVENT_END = re.compile(r'(?:举办|举行|召开|开幕|闭幕)$')
_POLICY_TITLE_SITE = re.compile(
    r'[_＿\-—\s]*(?:中国政府网|国务院部门文件|国务院文件|政策解读|电子政务).*$')
_POLICY_TITLE_DATE = re.compile(r'[（(]内容发布日期：[^）)]*[）)]')


def policy_title_candidate(title: str) -> str:
    """Return an evidence-backed policy-document title, excluding meeting and article titles."""
    if not title:
        return ''
    text = _POLICY_TITLE_DATE.sub('', str(title)).strip()

    text = re.split(r'[_＿]', text, 1)[0].strip()
    text = _POLICY_TITLE_SITE.sub('', text).strip()
    matches = [match for match in re.findall(r'《([^《》\n]{3,120})》', text)
               if _POLICY_TITLE_SUFFIX.search(match)]
    if matches:
        return max(matches, key=len).strip()

    if '公布' in text:
        text = text.rsplit('公布', 1)[-1].strip(' ：:')
    if len(text) > 120 or not _POLICY_TITLE_SUFFIX.search(text):
        return ''


    if not _looks_like_policy_document(text):
        return ''
    return text.strip(' ：:')


_ISSUER_REGION = re.compile(r'^([一-鿿]{2,12}?)(省|市|区|县|自治区|盟|州|镇|街道)')
_ISSUER_AGENCY = re.compile(r'^(?:中华人民共和国|中共中央|国务院|全国|国家|中央)')
_ISSUER_DEPARTMENT = re.compile(r'^[一-鿿]{2,10}(?:部|委|局|政府)')
_LAW_FORM = re.compile(r'(?:实施条例|条例|法律)$')


def _looks_like_policy_document(text: str) -> bool:
    value = str(text or '').strip()
    if not value:
        return False
    if '关于' in value or _LAW_FORM.search(value):
        return True
    if _ISSUER_AGENCY.match(value) or _ISSUER_DEPARTMENT.match(value):
        return True
    region = _ISSUER_REGION.match(value)
    return bool(region and admin_region_label(region.group(1) + region.group(2)))


def meeting_title_candidate(title: str) -> str:
    """Return a grounded meeting or training title when the source title identifies one."""
    if not title:
        return ''
    text = _POLICY_TITLE_DATE.sub('', str(title)).strip()
    text = re.split(r'[_＿]', text, 1)[0].strip()
    text = _POLICY_TITLE_SITE.sub('', text).strip()
    if len(text) > 100:
        return ''
    if _MEETING_SUFFIX.search(text):
        return text.strip(' ：:')

    if _MEETING_EVENT_END.search(text) and re.search(
            r'(会议|培训班|推进会|座谈会|研讨会|论坛|大会|工作会|部署会|交流会|发布会|宣讲会|联席会)', text):
        return text.strip(' ：:')
    return ''


_INSPECTION_TITLE = re.compile(r'(?:赴|到|前往|深入).{1,24}(?:调研|考察|走访|慰问|检查|督导|接访)')


def inspection_title_candidate(title: str) -> str:
    """Identify a source-title inspection event so explicit activity relations have an endpoint."""
    if not title:
        return ''
    text = _POLICY_TITLE_DATE.sub('', str(title)).strip()
    text = re.split(r'[_＿]', text, 1)[0].strip()
    text = _POLICY_TITLE_SITE.sub('', text).strip()
    if not text or len(text) > 60:
        return ''
    return text if _INSPECTION_TITLE.search(text) else ''


_REGION_SUFFIX_LABELS = (
    ('特别行政区', '市级机构'), ('自治区', '省级机构'), ('自治州', '市级机构'),
    ('街道', '街镇机构'), ('地区', '市级机构'), ('省', '省级机构'),
    ('市', '市级机构'), ('区', '区级机构'), ('县', '区级机构'),
    ('镇', '街镇机构'), ('乡', '街镇机构'),
)
_NON_ADMIN_AREA_SUFFIXES = (
    '社区', '小区', '园区', '校区', '景区', '厂区', '摊区', '片区',
    '街区', '居民区', '生活区', '作业区', '示范区', '试验区', '保护区', '风景区',
    '工业区', '商业区', '居住区', '营区',
)


def admin_region_label(text: str) -> str | None:
    """Recognize high-confidence administrative region suffixes, excluding nonadministrative areas."""
    value = str(text or '').strip()
    if len(value) < 3 or len(value) > 10 or value in GENERIC_REGION_NAMES:
        return None
    if value.endswith(_NON_ADMIN_AREA_SUFFIXES):
        return None
    for suffix, label in _REGION_SUFFIX_LABELS:
        if value.endswith(suffix) and len(value) > len(suffix) + 1:
            return label
    return None


def is_public_institution(text: str) -> bool:
    """Identify public institutions that must not be typed as regional government bodies."""
    return bool(text) and str(text).endswith(PUBLIC_INSTITUTION_SUFFIXES)


_POLICY_FORM = re.compile(
    r'(?:条例|办法|意见|通知|方案|规定|规范|决定|规划|纲要|细则|公告|公报|决议|'
    r'指引|标准|法律|措施|政策措施|行动计划|实施方案)(?:[（(][^）)]*[）)])?$')
_NON_POLICY_FA = {
    '办法', '做法', '想法', '说法', '方法', '用法', '写法', '打法', '疗法',
    '语法', '句法', '司法', '执法', '立法', '违法', '合法', '无法', '守法',
    '依法', '变法', '手法', '技法', '兵法', '宪法学',
}


def valid_policy_text(text: str) -> bool:
    """Require a plausible policy document title and exclude publications or commentary."""
    value = str(text or '').strip()
    inner = value.strip('《》').strip()
    if len(inner) < 3:
        return False
    if _POLICY_FORM.search(inner):
        return True
    if '公告' in inner or '公报' in inner:
        return True
    if inner.endswith('法') and len(inner) >= 5 and inner not in _NON_POLICY_FA:
        return True
    return False


_LABEL_PRIORITY = (
    '国家级政策', '省级政策', '市级政策', '区级政策', '街镇级政策', '政策征求意见稿',
    '国家级机构', '省级机构', '市级机构', '区级机构', '街镇机构',
    '领导干部', '人物',
    '金融机构', '金融产品', '交易场所', '经济指标',
    '境外国家/地区', '境外政府机构', '国际组织', '境外组织',
    '企业', '社区自治组织', '社会组织', '政府机构（含党组织）', '机构组织',
    '会议论坛', '调研视察', '居民活动',
    '街镇行动', '社区行动', '区级行动', '市级行动', '行动事项', '事件',
    '政策文件', '治理机制', '数字平台系统', '社会群体', '概念议题', '通用名词', '职务',
    '招聘公告', '认定仲裁公告',
)
_LABEL_RANK = {label: len(_LABEL_PRIORITY) - index for index, label in enumerate(_LABEL_PRIORITY)}
_FINANCIAL_SUFFIXES = ('银行', '保险公司', '证券公司', '基金公司', '基金管理有限公司',
                       '资产管理公司')
_NON_REGION_ORG_SUFFIXES = ('公司', '集团', '企业', '银行', '学校', '医院', '研究院',
                            '社区', '小区', '居委会', '业委会')


def dedupe_entities(entities: List[Any]) -> List[Any]:
    """Merge conflicting labels for the same entity name within one original block."""
    chosen: Dict[str, Any] = {}
    merged_tags: Dict[str, set] = {}
    for entity in entities:
        text = entity.text
        tags = set(getattr(entity, 'tags', ()))
        merged_tags.setdefault(text, set()).update(tags)
        current = chosen.get(text)
        if current is None:
            chosen[text] = entity
            continue
        candidates = {current.label, entity.label}
        region_label = admin_region_label(text)
        if current.label == entity.label:
            preferred = current.label
        elif text in KNOWN_ENTITY_TYPES and KNOWN_ENTITY_TYPES[text] in candidates:
            preferred = KNOWN_ENTITY_TYPES[text]
        elif region_label and region_label in candidates:
            preferred = region_label
        elif is_public_institution(text) and '机构组织' in candidates:
            preferred = '机构组织'
        elif text.endswith(_FINANCIAL_SUFFIXES) and '金融机构' in candidates and text != '中国人民银行':
            preferred = '金融机构'
        elif (region_label is None and candidates & REGION_LABELS
              and text.endswith(_NON_REGION_ORG_SUFFIXES)):

            non_region = candidates - REGION_LABELS
            preferred = max(non_region, key=lambda label: _LABEL_RANK.get(label, -1))
        else:
            preferred = max(candidates, key=lambda label: _LABEL_RANK.get(label, -1))
        chosen[text] = entity if entity.label == preferred else current
    result = []
    for text, entity in chosen.items():
        tags = tuple(sorted(merged_tags.get(text, set())))
        result.append(dataclasses.replace(entity, tags=tags))
    return result


def canonical_entity_name(text: str, label: str) -> str:
    """Normalize a node name using document punctuation and explicit organization aliases."""
    value = str(text or '').strip()
    if label in POLICY_LABELS or value.startswith('《'):
        value = value.strip('《》').strip()
        value = re.sub(r'\s+', '', value)
        value = re.sub(r'[（(][^（）()]{0,30}[）)]$', '', value)
        return value or str(text or '').strip()
    return ORG_CANONICAL.get(value, value) if label in ORG_LABELS else value


_GEO_LEVEL = {'国家级机构': 0, '省级机构': 1, '市级机构': 2,
              '区级机构': 3, '街镇机构': 4}


def geo_level(text: str, label: str) -> int | None:
    """Return the geographic level only for an actual administrative region."""
    value = str(text or '').strip()
    if value in {'国家', '中国', '中华人民共和国'}:
        return 0
    if label not in _GEO_LEVEL:
        return None
    if admin_region_label(value) or (label in ('省级机构', '市级机构')
                                     and 2 <= len(value) <= 6
                                     and value not in GENERIC_REGION_NAMES):
        return _GEO_LEVEL[label]
    return None


_BYLINE_MARKER = re.compile(
    r'(编辑|责编|监制|统筹|记者|撰稿|来源|制作|摄影|校对|审核|策划|值班|美编|签发)')


def is_byline_only(text: str, context: str) -> bool:
    """Treat names appearing exclusively in production bylines as attribution noise."""
    if not text or not context:
        return False
    lines = [line for line in str(context).splitlines() if text in line]
    return bool(lines) and all(_BYLINE_MARKER.search(line) for line in lines)


def classify_content(title: str, focus_id: int | None, entity_count: int,
                     relation_count: int) -> str:
    """Classify content deterministically for downstream diagnostics without changing extraction."""
    text = str(title or '')
    if re.search(r'(动态|速递|摘要|一周|早知道|盘点|汇总|集锦|资讯|周报)', text):
        return 'digest'
    if focus_id == 11 or policy_title_candidate(text):
        return 'policy'
    if (meeting_title_candidate(text) or inspection_title_candidate(text)
            or re.search(r'(举办|召开|举行|开展|启动|揭牌|签约|学习贯彻|专题学习|集中学习|志愿服务)', text)):
        return 'activity'
    if re.search(r'(报告|统计|数据发布|发布会)', text):
        return 'report'
    if re.search(r'(评论|观察|启示|思考|党课|论坛|笔谈|综述|述评|调研报告|工作法|实践与思考)', text):
        return 'commentary'
    density = relation_count / entity_count if entity_count else 0
    if entity_count and density < 0.08:
        return 'commentary'
    return 'news'


def quality_flags(title: str, published_date: str, content_type: str,
                  entity_count: int, relation_count: int) -> list[str]:
    """Return explainable quality flags without deleting sparse original evidence."""
    flags = []
    if content_type in ('digest', 'commentary', 'report'):
        flags.append(content_type)
    if entity_count and relation_count == 0:
        flags.append('zero_relation')
    if entity_count and relation_count / entity_count < 0.08:
        flags.append('low_density')
    if not published_date:
        flags.append('no_date')
    return sorted(set(flags))


_TITLE_CORE = (
    "中共中央总书记|国家主席|副总理|总书记|总理|委员长|省长|省长助理|"
    "市长|区长|县长|镇长|书记|部长|厅长|局长|处长|科长|主任|秘书长|"
    "主席|董事长|总经理|院长|校长|所长|站长|队长"
)


_SFX_TITLE = re.compile(rf"^(.{{2,4}}?)((?:副总?)?({_TITLE_CORE}))$")
_PFX_COMPOUND = re.compile(rf"^([一-龥]{{2,10}}(?:副总?)?({_TITLE_CORE}))([一-龥]{{2,4}})$")
_PFX_COMPOUND_PUNCT = re.compile(
    rf"^([一-龥、，, ]{{3,24}}(?:{_TITLE_CORE}))([一-龥]{{2,4}})$")
_ORG_HINT_CHARS = re.compile(r"[委政府部局街道办会团馆院校所院]$")
_SWB = re.compile(r"^.{0,8}社会工作(部)?$")


def _is_bare_name(name: str) -> bool:
    """Check whether a name after title removal remains a plausible short person name."""
    return 2 <= len(name) <= 4 and not _ORG_HINT_CHARS.search(name)


_POST_PERSON_GAP = r'[\s，,、:：]{0,4}'


def _mint_post_person_edges(out: List[Any], minted: List[Any], context: str) -> None:
    """Mint a current-post edge only for explicit adjacent post/person syntax in one sentence."""
    posts = [e for e in out if e.label == '职务' and qualified_post(e.text)]
    persons = [e for e in out if e.label in PERSON_LABELS]
    if not posts or not persons or not context:
        return
    seen = {(m.subject, m.predicate, m.object) for m in minted}
    sentences = [part for part in re.split(r'[。；！？\n]', context) if part.strip()]
    for sentence in sentences:
        present_posts = [p for p in posts if p.text in sentence]
        present_persons = [p for p in persons if p.text in sentence]
        if not present_posts or not present_persons:
            continue
        person_names = [p.text for p in present_persons]
        for post in present_posts:
            for person in present_persons:
                if (person.text in post.text or post.text in person.text
                        or not _post_person_adjacent(sentence, post.text,
                                                     person.text, person_names)):
                    continue
                key = (person.text, '现任', post.text)
                if key not in seen:
                    seen.add(key)
                    minted.append(_Triplet(subject=person.text, predicate='现任',
                                           object=post.text))


def _post_person_adjacent(sentence: str, post: str, person: str,
                          person_names: List[str]) -> bool:
    gap = _POST_PERSON_GAP

    if re.search(re.escape(post) + gap + re.escape(person), sentence):
        return True

    for match in re.finditer(re.escape(person) + gap + re.escape(post), sentence):
        after = sentence[match.end():match.end() + 8]
        if any(re.match(gap + re.escape(other), after)
               for other in person_names if other != person):
            continue
        return True
    return False


def normalize_entities(
    entities: List[Any], concepts: set, context: str = ''
) -> Tuple[List[Any], Dict[str, str], List[Any]]:
    """Normalize schema-gated entities, returning entities, evidence aliases and deterministic assertions."""
    out: List[Any] = []
    alias: Dict[str, str] = {}
    minted: List[Any] = []
    posts: Dict[str, Any] = {}

    for e in entities:
        label = strip_hint(e.label)
        text = e.text.strip()
        e = dataclasses.replace(e, text=text, label=label)

        if text in GENERIC_PHRASES or re.fullmatch(r'[\d\W_]+', text) or re.fullmatch(r'\d+月份?新闻发布会', text):
            continue
        canonical = canonical_reference(text, context)
        if not canonical:
            continue
        if canonical != text:
            alias[text] = canonical
            text = canonical
            e = dataclasses.replace(e, text=text)


        if label in POLICY_LABELS:
            canonical_text = canonical_policy_text(text)
        elif label in EVENT_TEXT_LABELS:
            canonical_text = canonical_event_text(text)
        else:
            canonical_text = text
        if canonical_text != text:
            alias[text] = canonical_text
            text = canonical_text
            e = dataclasses.replace(e, text=text)
        if text in TYPE_ONLY_NOUNS:
            continue
        overseas = _OVERSEAS_PREFIX.match(text)
        if (text in OVERSEAS_ORGS or text in OVERSEAS_COUNTRIES or text in OVERSEAS_REGIONS
                or text in OVERSEAS_GOVS or text in INTERNATIONAL_ORGS
                or (label in ORG_LABELS and overseas)):


            label = _overseas_label(text, getattr(e, 'tags', ()), concepts)
        if label in ORG_LABELS and (
                text in AGREEMENTS or text.endswith(_AGREEMENT_SUFFIX)):

            label = '概念议题'
        if label == '境外组织' and not overseas and text not in OVERSEAS_ORGS:
            tags = getattr(e, 'tags', ())
            if (re.search(r'[（(]中国[)）]', text)
                    or any(dimension == 'ownership' and value == '外资'
                           for dimension, value in tags)):
                financial = any(dimension == 'function' and value in _FINANCIAL_FUNCTIONS
                                for dimension, value in tags)
                label = '金融机构' if financial and '金融机构' in concepts else '企业'
        if text in DOMESTIC_COUNTRY_NAMES:

            label = '国家级机构'
        elif label in ('境外组织', '境外国家/地区', '境外政府机构', '国际组织') and _DOMESTIC_SAR.match(text):

            if text == '台湾':
                label = '省级机构'
            elif text in {'澳门', '香港'} or _SAR_GOV_SUFFIX.search(text):
                label = '市级机构'
            else:
                label = '机构组织'
        elif text in {'公安', '司法'}:
            label = '通用名词'
        elif text in NATIONAL_BODIES:
            label = '国家级机构'
        elif text == '彩凤鼓乐队':
            label = '居民活动'

        if text in DROP_SCOPE_ENTITIES:

            continue
        if text in KNOWN_ENTITY_TYPES and KNOWN_ENTITY_TYPES[text] in concepts:
            label = KNOWN_ENTITY_TYPES[text]
        if label in POLICY_LABELS and not valid_policy_text(text):

            if is_public_institution(text) or text.endswith(('报', '刊', '台', '社')):
                label = '机构组织'
            else:
                continue
        promoted = admin_region_label(text)
        if (promoted and label not in REGION_LABELS and label not in OVERSEAS_LABELS
                and not _OVERSEAS_PREFIX.match(text) and not _DOMESTIC_SAR.match(text)):


            label = promoted
        if label in REGION_LABELS and is_public_institution(text):

            label = '机构组织'
        if label in ORG_LABELS and text in ORG_CANONICAL:
            canonical_org = ORG_CANONICAL[text]
            if canonical_org != text:
                alias[text] = canonical_org
                text = canonical_org
                e = dataclasses.replace(e, text=text)
        if label == '会议论坛' and text in BARE_MEETINGS:

            continue
        e = dataclasses.replace(e, label=label)

        if (label in {'行动事项', '市级行动', '区级行动', '街镇行动', '社区行动', '事件'}
                and _MEETING_SUFFIX.search(text) and '会议论坛' in concepts):


            label = '会议论坛'
            e = dataclasses.replace(e, label=label)

        if label == '调研视察' and not INSPECTION_VERB_RE.search(text):


            replacement = ('行动事项' if '行动事项' in concepts
                           else '事件' if '事件' in concepts else '')
            if not replacement:
                continue
            label = replacement
            e = dataclasses.replace(e, label=label)

        if label == '治理机制' and text in GENERIC_MECHANISMS:
            continue
        if label == '概念议题' and text in GENERIC_CONCEPTS:
            continue

        if label in REGION_LABELS:
            canonical_region = canonical_region_name(text, label)
            if canonical_region != text:
                alias[text] = canonical_region
                text = canonical_region
                e = dataclasses.replace(e, text=text)
        city = getattr(e, 'city', None)
        if city:
            canonical_city = canonical_city_name(city)
            if canonical_city != city:
                e = dataclasses.replace(e, city=canonical_city)

        if label in ORG_LABELS and _SWB.match(text):
            e = dataclasses.replace(e, text=text, label="政府机构（含党组织）")
            label = e.label

        if label in ORG_LABELS:
            qualified = qualify_organization(text, context, city=getattr(e, 'city', None), district=getattr(e, 'district', None))
            if not qualified:
                continue
            if qualified != text:
                alias[text] = qualified
                text = qualified
                e = dataclasses.replace(e, text=text)

        if label in PERSON_LABELS:
            new_text, title = _strip_title(text)
            if new_text != text:
                alias.setdefault(text, new_text)
                e = dataclasses.replace(e, text=new_text)
                if title and qualified_post(title) and "职务" in concepts:


                    posts.setdefault(
                        title,
                        dataclasses.replace(e, text=title, label="职务"),
                    )
                    minted.append(
                        _Triplet(subject=new_text, predicate="现任", object=title)
                    )
            text = e.text
            if is_byline_only(text, context):

                continue
            if (re.fullmatch(r'(?:小|老|阿)[一-龥]{1,2}|[一-龥]+(?:某某|某|先生|女士|同志)', text)
                    or text in {'某某', '某人', '一名居民'} or not _is_bare_name(text)):
                continue

        if label == '职务' and not qualified_post(text):
            continue

        if label == "社会群体" and text in BARE_GROUPS:
            continue

        if text in BARE_ORGS:
            continue

        if label not in concepts:
            continue

        out.append(e)

    for p in posts.values():
        out.append(p)

    out = dedupe_entities(out)
    _mint_location_edges(out, minted, context)
    _mint_post_person_edges(out, minted, context)
    return out, alias, minted


_EXCLUDED_REGIONS = {'中国', '全国', '中央', '国家'}
_ADMIN_LEVEL = {'国家级机构': 0, '省级机构': 1, '市级机构': 2,
                '区级机构': 3, '街镇机构': 4}


def _overseas_label(text, tags, concepts):
    """Use a finer overseas type only when the active domain schema defines it."""
    if '境外国家/地区' not in concepts:
        return '境外组织'
    if text in INTERNATIONAL_ORGS or any(word in text for word in INTERNATIONAL_WORDS):
        return '国际组织'
    if text in OVERSEAS_GOVS or (_OVERSEAS_PREFIX.match(text) and _GOV_SUFFIX.search(text)):
        return '境外政府机构'
    if text in OVERSEAS_COUNTRIES or text in OVERSEAS_REGIONS:
        return '境外国家/地区'
    return '境外组织'


def canonical_region_name(text: str, label: str) -> str:
    """Expand known administrative region names to their explicit canonical forms."""
    if label == '省级机构':
        return PROVINCE_CANONICAL.get(text, text)
    if label == '市级机构':
        if text in CITY_CANONICAL:
            return CITY_CANONICAL[text]
        if text in CITY_NAMES and not text.endswith(_CITY_SUFFIXES):
            return text + '市'
    return text


def canonical_city_name(city: str) -> str:
    if city in PROVINCE_CANONICAL:
        return PROVINCE_CANONICAL[city]
    if city in CITY_CANONICAL:
        return CITY_CANONICAL[city]
    if city in CITY_NAMES and not city.endswith(_CITY_SUFFIXES):
        return city + '市'
    return city


def _region_core(text: str) -> str:
    for suffix in sorted(_REGION_LEVEL, key=len, reverse=True):
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[:-len(suffix)]
    return text if 2 <= len(text) <= 6 and not _AGENCY_SUFFIX.search(text) else ''


def region_level(text: str, label: str | None = None) -> int | None:
    """Infer administrative level from a supported suffix or an explicit schema label."""
    for suffix in sorted(_REGION_LEVEL, key=len, reverse=True):
        if text.endswith(suffix) and len(text) > len(suffix):
            return _REGION_LEVEL[suffix]
    if label in ('省级机构', '市级机构') and len(text) <= 6 and not _AGENCY_SUFFIX.search(text):
        return _ADMIN_LEVEL[label]
    return None


def region_priority(text: str, label: str | None = None) -> int:
    for suffix in sorted(_LOCATION_PRIORITY, key=len, reverse=True):
        if text.endswith(suffix) and len(text) > len(suffix):
            return _LOCATION_PRIORITY[suffix]
    if label in ('省级机构', '市级机构') and len(text) <= 6 and not _AGENCY_SUFFIX.search(text):
        return 2 if label == '省级机构' else 3
    return 0


def is_region(text: str, label: str) -> bool:
    """Distinguish administrative region names from organizations that contain such names."""
    return (label in REGION_LABELS and text not in _EXCLUDED_REGIONS
            and region_level(text, label) is not None)


def admin_level(label: str) -> int | None:
    return _ADMIN_LEVEL.get(label)


def _is_region_entity(entity) -> bool:
    return is_region(entity.text, entity.label)


def _mentioned_in(text: str, sentence: str) -> bool:
    """Match qualified entity names to the longest grounded suffix in a source sentence."""
    if text in sentence:
        return True
    return any(text[-size:] in sentence for size in range(len(text) - 1, 2, -1))


def _location_target(entity, candidates):
    """Prefer a region explicitly in the entity name and reject implausibly finer locations."""
    level = admin_level(entity.label)
    matched = [r for r in candidates if (core := _region_core(r.text)) and core in entity.text]
    pool = matched or candidates
    if level is not None:

        pool = [r for r in pool
                if (target_level := region_level(r.text, r.label)) is None or target_level <= level]
    if not pool:
        return None
    return max(pool, key=lambda r: (region_priority(r.text, r.label), len(r.text)))


def _mint_location_edges(out: List[Any], minted: List[Any], context: str) -> None:
    """Mint location or hierarchy assertions only from explicit original region evidence."""
    regions = [e for e in out if _is_region_entity(e)]
    if not regions:
        return
    by_text = {e.text: e for e in out}
    sentences = [part for part in re.split(r'[。；！？\n]', context) if part.strip()]
    seen = {(m.subject, m.predicate, m.object) for m in minted}

    def add(subject: str, predicate: str, obj: str) -> None:
        key = (subject, predicate, obj)
        if subject != obj and key not in seen:
            seen.add(key)
            minted.append(_Triplet(subject=subject, predicate=predicate, object=obj))

    region_texts = {r.text for r in regions}
    action_labels = {'行动事项', '市级行动', '区级行动', '街镇行动', '社区行动',
                     '居民活动', '事件', '会议论坛'}
    for entity in out:
        if entity.text in region_texts:
            continue

        candidates = [r for r in regions
                      if (core := _region_core(r.text)) and core in entity.text]
        if entity.label in action_labels and not candidates:

            continue
        if entity.label not in ORG_LABELS and entity.label not in action_labels:
            continue
        if not candidates:


            district, city = getattr(entity, 'district', None), getattr(entity, 'city', None)
            for sentence in sentences:
                if not _mentioned_in(entity.text, sentence):
                    continue
                found = [r for r in regions if r.text in sentence]
                if not found:
                    continue
                jurisdiction = [by_text[name] for name in (district, city)
                                if name in region_texts and by_text[name] in found]
                pos = sentence.find(entity.text)
                prefix = sentence[:pos] if pos > 0 else ''
                prefix_matches = [r for r in found
                                  if prefix.endswith(r.text)
                                  or ((core := _region_core(r.text)) and prefix.endswith(core))]
                if jurisdiction:
                    candidates = jurisdiction
                elif prefix_matches:
                    candidates = found
                break
        target = _location_target(entity, candidates) if candidates else None
        if target is not None:
            add(entity.text, '位于', target.text)


            title = context.split('\n', 1)[0] if '\n' in context else ''
            title_regions = [r for r in regions
                             if title and (r.text in title
                                           or (_region_core(r.text) and _region_core(r.text) in title))]
            if title_regions:
                broader = max(title_regions,
                              key=lambda r: (region_level(r.text, r.label) or 0, len(r.text)))
                broader_level = region_level(broader.text, broader.label)
                target_level = region_level(target.text, target.label)
                local = any((core := _region_core(r.text)) and core in entity.text for r in candidates)


                if (broader.text != target.text and local and target_level == 4
                        and broader_level in (2, 3) and broader_level < target_level):
                    add(entity.text, '位于', broader.text)



    for sentence in sentences:
        hits, spans = [], set()
        for region in regions:
            pos = sentence.find(region.text)
            span = len(region.text) if pos >= 0 else None
            if pos < 0:
                core = _region_core(region.text)
                pos = sentence.find(core) if len(core) >= 2 else -1
                span = len(core) if pos >= 0 else None
            if pos >= 0 and (pos, span) not in spans:
                spans.add((pos, span))
                hits.append((pos, span, region))
        hits.sort(key=lambda item: item[0])
        for (parent_pos, parent_span, parent), (child_pos, _, child) in zip(hits, hits[1:]):
            parent_level = region_level(parent.text, parent.label)
            child_level = region_level(child.text, child.label)
            if (parent_level is None or child_level is None or child_level <= parent_level
                    or child_pos != parent_pos + parent_span):
                continue
            add(child.text, '隶属', parent.text)


def _strip_title(text: str) -> Tuple[str, str]:
    """Separate an explicit person name and qualified post without inventing either."""
    m = _PFX_COMPOUND_PUNCT.match(text)
    if m and _is_bare_name(m.group(2)) and qualified_post(m.group(1)):
        return m.group(2), m.group(1)
    m = _PFX_COMPOUND.match(text)
    if m and _is_bare_name(m.group(3)):
        return m.group(3), m.group(1)
    m = _SFX_TITLE.match(text)
    if m and _is_bare_name(m.group(1)) and m.group(1) != m.group(2):
        return m.group(1), m.group(2)
    return text, ""


@dataclasses.dataclass(frozen=True)
class _Triplet:
    subject: str
    predicate: str
    object: str


PREDICATE_ALIASES = {

    '出席': '参与', '参加': '参与', '主持': '参与',
    '讲话': '强调', '发言': '强调', '致辞': '强调',
}


def normalize_predicate(pred: str) -> str:
    """Remove decorative hints from a model-returned relation label."""
    value = strip_hint(pred)
    return PREDICATE_ALIASES.get(value, value)
