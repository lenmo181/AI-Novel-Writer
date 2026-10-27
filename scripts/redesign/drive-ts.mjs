/* global process */
/**
 * drive.mjs 的启动包装：先注册 resolve 钩子（无扩展名导入 → .ts），
 * 再动态加载驱动本体。用法与 drive.mjs 完全一致：
 *   node scripts/redesign/drive-ts.mjs --scenario arch-regression
 */
import { register } from 'node:module'
import { fileURLToPath } from 'node:url'

register('./ts-resolve-hook.mjs', import.meta.url)
// drive.mjs 只在自己是入口（process.argv[1] 指向它）时跑 main；包装器代它做入口，把 argv[1] 指过去再加载。
process.argv[1] = fileURLToPath(new URL('./drive.mjs', import.meta.url))
await import('./drive.mjs')
