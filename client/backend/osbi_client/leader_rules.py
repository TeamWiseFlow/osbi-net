"""Deterministic leader locality rules for the Shanghai/Jing'an report preset.

Grassroots posts require an actual leader title. Concurrent city committee titles
follow their substantive district post. These rules rank this report preset only;
they never restrict which locations can enter the shared graph.
"""
from __future__ import annotations
import re

TARGET_CITY = '上海'
TARGET_DISTRICT = '静安'
LEADER_TITLE_TERMS = ('市委书记', '市委副书记', '市长', '副市长', '区委书记', '区委副书记', '区长', '副区长', '区委常委', '部长', '政法委书记', '人大常委会主任', '政协主席')
LEADER_VERBS = ('调研', '考察', '走访', '出席', '讲话', '强调', '致辞', '主持', '参加', '参观', '指出', '表示', '要求')
LEADER_ACTIVITY_RE = re.compile(f'(?:{'|'.join(LEADER_TITLE_TERMS)})[一-龥]{{1,8}}(?:{'|'.join(LEADER_VERBS)})')
NOTICE_MARKERS = ('活动时间', '活动地点', '指导单位', '主办单位', '协办单位', '扫描二维码', '点击蓝字', '预约')
CITY_LEADER_TERMS = ('市委书记', '市长', '副市长', '市委常委', '市委副书记', '上海市委')
CONCURRENT_CITY_TITLES = ('市委常委',)
DISTRICT_POST_TERMS = ('区委书记', '区委副书记', '区长', '副区长', '区委常委')
OTHER_DISTRICTS = ('黄浦区', '长宁区', '虹口区', '杨浦区', '普陀区', '徐汇区', '浦东新区', '闵行区', '宝山区', '嘉定区', '松江区', '青浦区', '奉贤区', '金山区', '崇明区')
OTHER_DISTRICT_SHORT = ('黄浦', '长宁', '虹口', '杨浦', '普陀', '徐汇', '浦东', '闵行', '宝山', '嘉定', '松江', '青浦', '奉贤', '金山', '崇明')
JINGAN_TERRITORY = (TARGET_DISTRICT, '临汾', '彭浦', '大宁', '共和新路', '宝山路', '芷江西路', '北站', '江宁路', '石门二路', '南京西路', '曹家渡', '静安寺', '天目西路')
OTHER_PROVINCES = ('北京', '天津', '重庆', '河北', '山西', '内蒙古', '辽宁', '吉林', '黑龙江', '江苏', '浙江', '安徽', '福建', '江西', '山东', '河南', '湖北', '湖南', '广东', '广西', '海南', '四川', '贵州', '云南', '西藏', '陕西', '甘肃', '青海', '宁夏', '新疆')
KNOWN_NATIONAL_LEADERS = {'习近平', '李强', '赵乐际', '王沪宁', '蔡奇', '丁薛祥', '李希', '韩正'}
NATIONAL_MARKERS = ('中共中央', '中央政治局', '国务院', '全国人大', '全国政协', '中央纪委', '中央军委', '国家主席', '总书记')
NATIONAL_TITLES = ('国务委员', '副总理', '全国人大常委会委员长', '全国政协主席', '中央政治局常委', '中央政治局委员', '中央书记处书记', '中央纪委书记', '中央军委主席')
LOCAL_POST_MARKERS = re.compile('(省委|市委|区委|县委|镇委|街道|自治区)')

def _sentences(text: str) -> list:
    return [s for s in re.split('[。；！？\\n]', text or '') if s.strip()]
_STREET_SUFFIX = set('路街道公桥寺院学医中广新小')

def _mentions_other_district(text: str, *, allow_short: bool) -> bool:
    if any((district in text for district in OTHER_DISTRICTS)):
        return True
    if not allow_short:
        return False
    for district in OTHER_DISTRICT_SHORT:
        for match in re.finditer(district, text):
            tail = text[match.end():match.end() + 1]
            if tail not in _STREET_SUFFIX:
                return True
    return False

def _concurrent_other_district(text: str) -> bool:
    if not any((title in text for title in CONCURRENT_CITY_TITLES)):
        return False
    if not _mentions_other_district(text, allow_short=False):
        return False
    return any((term in text for term in DISTRICT_POST_TERMS))

def _is_explicit_target(sentence: str) -> bool:
    has_title = any((term in sentence for term in LEADER_TITLE_TERMS))
    if not has_title:
        return False
    if _concurrent_other_district(sentence):
        return False
    if TARGET_DISTRICT in sentence:
        if not _mentions_other_district(sentence, allow_short=False):
            return True
        return TARGET_CITY in sentence and any((term in sentence for term in CITY_LEADER_TERMS))
    return TARGET_CITY in sentence and any((term in sentence for term in CITY_LEADER_TERMS))

def _is_explicit_other(sentence: str) -> bool:
    if _mentions_other_district(sentence, allow_short=False):
        return True
    if any((province in sentence for province in OTHER_PROVINCES)):
        return True
    return '省' in sentence or '自治区' in sentence

def _context_is_target(context: str) -> bool:
    target = TARGET_DISTRICT in context or TARGET_CITY in context or any((place in context for place in JINGAN_TERRITORY))
    if not target:
        return False
    other = _mentions_other_district(context, allow_short=True) or any((province in context for province in OTHER_PROVINCES))
    return not other

def _is_national(sentence: str) -> bool:
    if any((name in sentence for name in KNOWN_NATIONAL_LEADERS)):
        return True
    if any((marker in sentence for marker in NATIONAL_MARKERS)):
        return True
    if any((title in sentence for title in NATIONAL_TITLES)):
        return True
    return '部部长' in sentence and (not LOCAL_POST_MARKERS.search(sentence))

def _sentence_category(sentence: str, context: str) -> str | None:
    if any((marker in sentence for marker in NOTICE_MARKERS)):
        return None
    if not LEADER_ACTIVITY_RE.search(sentence):
        return None
    if _is_explicit_target(sentence):
        return 'target'
    if _is_national(sentence):
        return 'national'
    if _is_explicit_other(sentence):
        return 'other'
    return 'target' if _context_is_target(context) else 'other'

def classify_leader_text(text: str) -> set:
    text = text or ''
    context = text[:300]
    categories = set()
    for sentence in _sentences(text):
        category = _sentence_category(sentence, context)
        if category:
            categories.add(category)
    return categories

def has_leader_activity(text: str) -> bool:
    return bool(classify_leader_text(text))

def is_target_leader(text: str) -> bool:
    return 'target' in classify_leader_text(text)

def is_national_leader(text: str) -> bool:
    categories = classify_leader_text(text)
    return 'national' in categories and 'target' not in categories

def is_non_target_leader(text: str) -> bool:
    categories = classify_leader_text(text)
    return 'other' in categories and 'target' not in categories and ('national' not in categories)

def is_target_leader_sentence(sentence: str, context: str='') -> bool:
    return _sentence_category(sentence, context or sentence) == 'target'

def is_target_jurisdiction(text: str) -> bool:
    text = text or ''
    if _concurrent_other_district(text):
        return False
    if TARGET_DISTRICT in text and any((term in text for term in LEADER_TITLE_TERMS)):
        return True
    if TARGET_CITY in text and any((term in text for term in CITY_LEADER_TERMS)):
        return True
    if any((term in text for term in LEADER_TITLE_TERMS)) and _context_is_target(text[:300]):
        return True
    return False
NATIONAL_LOCALITY_MARKERS = ('国家', '全国', '中央', '国务院', '中共中央', '全国人大', '全国政协')

def _locality_tier_from_text(text: str) -> str:
    text = str(text or '')
    if any((province in text for province in OTHER_PROVINCES)):
        return 'other_province'
    if '省' in text or '自治区' in text:
        return 'other_province'
    if any((place in text for place in JINGAN_TERRITORY)):
        return 'target'
    if _mentions_other_district(text, allow_short=True):
        return 'other_district'
    if any((marker in text for marker in NATIONAL_LOCALITY_MARKERS)):
        return 'national'
    if TARGET_CITY in text or '市委' in text or '市政府' in text:
        return 'shanghai'
    return ''

def locality_tier(text: str, publisher: str='') -> str:
    text = str(text or '')
    for candidate in (publisher, text[:200], text):
        tier = _locality_tier_from_text(candidate)
        if tier:
            return tier
    return 'unknown'

def target_leader_snippet(text: str, limit: int=160) -> str:
    text = text or ''
    context = text[:300]
    for sentence in _sentences(text):
        if _sentence_category(sentence, context) == 'target':
            return sentence.strip()[:limit]
    return ''


def configure(*, targetCity=None, targetDistrict=None, targetTerritory=None,
              cityLeaderTerms=None, otherDistricts=None, otherDistrictShort=None,
              otherProvinces=None, knownNationalLeaders=None, nationalMarkers=None,
              nationalTitles=None, leaderTitleTerms=None, leaderVerbs=None,
              concurrentCityTitles=None, districtPostTerms=None):
    """Override node locality/leader rules. Omitted values keep the defaults."""
    global TARGET_CITY, TARGET_DISTRICT, JINGAN_TERRITORY, CITY_LEADER_TERMS
    global OTHER_DISTRICTS, OTHER_DISTRICT_SHORT, OTHER_PROVINCES
    global KNOWN_NATIONAL_LEADERS, NATIONAL_MARKERS, NATIONAL_TITLES
    global LEADER_TITLE_TERMS, LEADER_VERBS, LEADER_ACTIVITY_RE
    global CONCURRENT_CITY_TITLES, DISTRICT_POST_TERMS
    if targetCity:
        TARGET_CITY = str(targetCity)
    if targetDistrict:
        TARGET_DISTRICT = str(targetDistrict)
    if targetTerritory:
        JINGAN_TERRITORY = tuple(str(value) for value in targetTerritory)
    if cityLeaderTerms:
        CITY_LEADER_TERMS = tuple(str(value) for value in cityLeaderTerms)
    if otherDistricts:
        OTHER_DISTRICTS = tuple(str(value) for value in otherDistricts)
    if otherDistrictShort:
        OTHER_DISTRICT_SHORT = tuple(str(value) for value in otherDistrictShort)
    if otherProvinces:
        OTHER_PROVINCES = tuple(str(value) for value in otherProvinces)
    if knownNationalLeaders:
        KNOWN_NATIONAL_LEADERS = set(knownNationalLeaders)
    if nationalMarkers:
        NATIONAL_MARKERS = tuple(str(value) for value in nationalMarkers)
    if nationalTitles:
        NATIONAL_TITLES = tuple(str(value) for value in nationalTitles)
    if leaderTitleTerms:
        LEADER_TITLE_TERMS = tuple(str(value) for value in leaderTitleTerms)
    if leaderVerbs:
        LEADER_VERBS = tuple(str(value) for value in leaderVerbs)
    if concurrentCityTitles:
        CONCURRENT_CITY_TITLES = tuple(str(value) for value in concurrentCityTitles)
    if districtPostTerms:
        DISTRICT_POST_TERMS = tuple(str(value) for value in districtPostTerms)
    LEADER_ACTIVITY_RE = re.compile(f'(?:{'|'.join(LEADER_TITLE_TERMS)})[一-龥]{{1,8}}(?:{'|'.join(LEADER_VERBS)})')
