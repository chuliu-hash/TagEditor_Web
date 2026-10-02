/* TagEditor_Web 四页共享的前端工具函数。
 *
 * 引入方：templates/ 下四个页面（tag_editor / image_editor / prompt_tool / danbooru_wiki）
 * 都在 <head> 里用 <script src="/static/js/tageditor-common.js"></script> 引入，
 * 必须先于页面内联脚本（行内 onclick 与初始化代码引用这里的函数）。
 *
 * **必须保持普通全局函数**：页面里大量行内 `onclick="fnName()"` 依赖全局作用域，
 * 改成 `type="module"` 会全部失效。
 *
 * 收录标准（别往里塞页面特定的东西）：
 *   - 语义在所有引入页面**完全一致**（或调用方按页面传参）—— 曾经 `showConfirm`
 *     在四页有三套变体，合并时逐字对比过函数体才搬，分叉的函数一律留在页面内
 *     （如 `renderDText` 依赖两页行为不同的 `_dtextInline`）。
 *   - 被测试断言从模板截函数体的函数**必须留在原地**（`tests/test_invariants.py`
 *     的 `js_of`/`_fn_body` 只读 templates/*.html）：prompt_tool 的
 *     `deleteSession`/`renderResult`/`renderDiff`/`renderImageGrid`/`clearResultPanel`/
 *     `loadSession`/`uploadImages`/`deleteImage`/`toggleImage`，image_editor 的
 *     `toggleBrushMode`/`toggleCropMode`/`enterBrushMode`/`doNavigate`/`bakeBrushToImg`。
 *
 * 页面需要提供的全局量（本文件只读不定义）：
 *   - `escCloseModal` 读 `_ESC_CLOSERS`（每页自己的「弹窗 id → 关闭函数」映射，
 *     调用时才解析，无加载时序问题）
 *   - `closeConfirm`/`confirmOk`/`confirmCancel` 读写 `_confirmOnOk`/`_confirmOnCancel`
 *     （页面级 let/var，showConfirm 在页面里赋值）
 */

// HTML 转义。**注意正则字面量一开一合**：`/"/g` 与 `/'/g` 会让朴素的引号状态机
// 带偏，`strip_js_comments`（tests/test_invariants.py）为此专门认正则字面量。
function escapeHtml(str) {
    return String(str == null ? '' : str)
        .replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')
        .replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// 右下角通知（3 秒自动消失）。`#notification` 元素四页都有。
let _notifyTimer = null;
function showNotification(message, type) {
    const n = document.getElementById('notification');
    const colors = {success:'bg-green-500 text-white',error:'bg-red-500 text-white',warning:'bg-amber-500 text-white',info:'bg-blue-500 text-white'};
    n.className = 'fixed bottom-5 right-5 px-5 py-3 rounded-lg shadow-lg flex items-center z-50 transition-all duration-300 text-sm ' + (colors[type]||colors.info);
    n.textContent = message;
    n.style.transform = 'translateY(0)'; n.style.opacity = '1';
    if (_notifyTimer) clearTimeout(_notifyTimer);
    _notifyTimer = setTimeout(() => { n.style.transform = 'translateY(20px)'; n.style.opacity = '0'; _notifyTimer = null; }, 3000);
}

// 是否有弹窗打开：所有弹窗都是「fixed inset-0 ... hidden」的遮罩层。
// 旧版不看弹窗状态，「未保存修改」确认框弹出时按 → 会在遮罩底下真的切图，
// 用户点「取消」已来不及。加新弹窗时沿用同一结构，这里就天然覆盖。
function _anyModalOpen() {
    const overlays = document.querySelectorAll('div.fixed.inset-0');
    for (let i = 0; i < overlays.length; i++) {
        if (!overlays[i].classList.contains('hidden')) return true;
    }
    return false;
}

// 统一确认弹窗的关闭/确认/取消（回调变量 _confirmOnOk/_confirmOnCancel 由页面
// 的 showConfirm 赋值，弹窗 DOM 四页同构：#confirm-modal / #confirm-ok-btn 等）。
function closeConfirm() {
    document.getElementById('confirm-modal').classList.add('hidden');
    _confirmOnOk = null;
    _confirmOnCancel = null;
}
function confirmOk() {
    const cb = _confirmOnOk;
    closeConfirm();
    if (cb) cb();
}
function confirmCancel() {
    const cb = _confirmOnCancel;
    closeConfirm();
    if (cb) cb();
}

// Esc 关掉最上层的弹窗。返回 true 表示吞掉按键（别让 Esc 再触发别的）。
// 关闭策略读页面级 `_ESC_CLOSERS`（弹窗 id → 关闭函数；null = 只关不调回调），
// 不在映射里的弹窗直接隐藏。
function escCloseModal() {
    if (!_anyModalOpen()) return false;   // 判定口径与快捷键守卫同一份，不另写
    const overlays = document.querySelectorAll('div.fixed.inset-0');
    let top = null;
    for (let i = 0; i < overlays.length; i++) {
        if (!overlays[i].classList.contains('hidden')) top = overlays[i];
    }
    if (!top) return false;
    if (Object.prototype.hasOwnProperty.call(_ESC_CLOSERS, top.id)) {
        const fn = _ESC_CLOSERS[top.id];
        if (fn) fn();
        return true;   // 吞掉按键：进度弹窗也吞，别让 Esc 再触发别的
    }
    top.classList.add('hidden');
    return true;
}

// 防抖（输入框实时搜索等场景用）。
function debounce(fn, wait) {
    let timer = null;
    return function() {
        clearTimeout(timer);
        const args = arguments, ctx = this;
        timer = setTimeout(function() { fn.apply(ctx, args); }, wait);
    };
}

// DText 图片缩略图懒加载（标签编辑页详情卡 + Danbooru 页详情卡共用）。
// .dtext-post-thumb[data-media-id] → Danbooru API 拿大图 URL；asset 类型的走两段兜底。
function _dtextLoadThumbnails(container) {
    const imgs = (container || document).querySelectorAll('.dtext-post-thumb[data-media-id]');
    imgs.forEach(function(img) {
        if (img.dataset.loaded) return;
        img.dataset.loaded = '1';
        const mediaId = img.dataset.mediaId;
        const mediaType = img.dataset.mediaType || 'post';
        if (mediaType === 'asset') {
            // !asset: 依次尝试 posts 搜索 → media_assets API → fallback 图标
            _loadAssetThumbnail(img, mediaId);
        } else {
            fetch('https://danbooru.donmai.us/posts/' + mediaId + '.json')
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    const src = data.large_file_url || data.preview_file_url || data.url;
                    if (src) {
                        img.src = src;
                        img.style.display = '';
                    } else {
                        img.style.display = 'none';
                    }
                })
                .catch(function() { img.style.display = 'none'; });
        }
    });
}

// asset 缩略图的两段兜底：posts 搜索（media_asset_id）→ media_assets API
// （variant_download_urls 的多档变体 → 通用字段 → md5 拼 CDN 预览 URL）。
function _loadAssetThumbnail(img, mediaId) {
    // 尝试1: posts 搜索（media_asset_id）
    fetch('https://danbooru.donmai.us/posts.json?tags=media_asset_id:' + mediaId + '&limit=1')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            const post = Array.isArray(data) ? data[0] : null;
            if (post && post.large_file_url) {
                img.src = post.large_file_url;
                img.style.display = '';
                return;
            }
            // 尝试2: media_assets API
            fetch('https://danbooru.donmai.us/media_assets/' + mediaId + '.json')
                .then(function(r2) { return r2.json(); })
                .then(function(data2) {
                    const ma = data2.media_asset || data2;
                    let src = null;
                    if (ma.variant_download_urls) {
                        const v = ma.variant_download_urls;
                        src = v.preview || v['180x180'] || v.large || v.original;
                    }
                    if (!src) src = ma.large_file_url || ma.preview_file_url || ma.file_url;
                    if (!src && ma.md5 && ma.file_ext) {
                        src = 'https://cdn.donmai.us/preview/' + ma.md5.substring(0,2) + '/' + ma.md5.substring(2,4) + '/' + ma.md5 + '.' + ma.file_ext;
                    }
                    if (src) {
                        img.src = src;
                        img.style.display = '';
                    } else {
                        img.style.display = 'none';
                    }
                })
                .catch(function() { img.style.display = 'none'; });
        })
        .catch(function() { img.style.display = 'none'; });
}
