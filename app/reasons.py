REASONS = {
    'COLUMN_COUNT': '列数与潮尺表头不符', 'EMPTY_FIELD': '关键字段为空或为哨兵',
    'BAD_READING': '读数、单位、时刻或质量标志不合法', 'SENSOR_BAD': '测站主动标为坏值',
    'NO_STATION': '站号未登记', 'CONFLICT': '同站同刻存在不同读数',
    'DUPLICATE': '同站同刻重复读数', 'SHADOWED': '同刻采用优先级更高的站',
    'OVER_GAP': '相邻有效读数间隔超过断面上限', 'NO_BRACKET': '断面缺少双侧读数',
    'STATION_DISABLED': '值班裁决停用整站，本站读数不进入判定',
    'RULED_EXCLUDE': '值班裁决只剔除指定坏行',
    'SCOPE_CHANGED': '晚到读数改变影响范围，原处置已作废',
    'DECISION_CONFLICT': '该异常已有不同处置确认',
    'UNDER_DEPTH': '至少一处断面低于放行水深',
    'DEPTH_OK': '所有断面均达到放行水深',
}
