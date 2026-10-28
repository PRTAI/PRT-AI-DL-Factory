/**
 * 项目筛选值与全局「当前项目」之间的换算。
 *
 * 顶栏切换器把当前项目存在 useProject（全局 store）里，页面上的下拉框用
 * `'all'` 表示「全部项目」、其余值时就是项目 id；全局 store 则用空串表示未选项目。
 * 两边互转集中在这里，避免各页面自己写一套把空串和 'all' 搞混。
 */
export const ALL_PROJECTS = 'all'

/** 全局当前项目 → 页面筛选值（未选项目时看全部）。 */
export function projectScopeValue(projectId: string): string {
  return projectId || ALL_PROJECTS
}

/** 页面筛选值 → 全局当前项目（「全部项目」清空当前项目）。 */
export function projectIdFromScope(scope: string): string {
  return scope === ALL_PROJECTS ? '' : scope
}
