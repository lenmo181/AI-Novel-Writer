/**
 * drive.mjs 直接 import 应用里的 .ts，而应用的 TS 相互导入时不写扩展名。
 * Node 自带的类型剥离能转译 .ts，但模块解析不会补扩展名；这个 resolve 钩子补上。
 * 用法：由 drive-ts.mjs 在 import drive.mjs 之前 register。
 */
export async function resolve(specifier, context, next) {
  if (specifier.startsWith('.') || specifier.startsWith('#')) {
    try {
      return await next(specifier, context)
    } catch (error) {
      if (error.code === 'ERR_MODULE_NOT_FOUND') {
        for (const suffix of ['.ts', '/index.ts', '.tsx']) {
          try {
            return await next(specifier + suffix, context)
          } catch {
            // 尝试下一个候选
          }
        }
      }
      throw error
    }
  }
  return next(specifier, context)
}
