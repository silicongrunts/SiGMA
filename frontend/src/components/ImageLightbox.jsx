/**
 * ImageLightbox - Fullscreen image viewer opened by clicking images in
 * markdown content (ChatShared's MarkdownContent, Preview's prose) and chat
 * attachment thumbnails (ChatPanel's AttachmentStrip).
 *
 * Displays the already-sanitized <img> src inside a fullscreen overlay:
 * wheel zooms around the cursor, buttons/keyboard zoom around the viewport
 * center, drag pans while zoomed, double-click toggles fit/2.5x, ESC or a
 * backdrop click closes. "Open original" keeps a direct path to the raw
 * image (hidden for data: URLs, which browsers refuse to open top-level).
 *
 * Portalled to <body> with the app's z-[5000] modal convention — callers sit
 * inside overflow-hidden/animated containers whose stacking contexts would
 * otherwise clip or trap a fixed-position child (same rationale as
 * ChatShared's FileEditModal).
 */
import { memo, useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useTranslation } from 'react-i18next'
import { AlertTriangle, ExternalLink, Loader2, Maximize2, X, ZoomIn, ZoomOut } from 'lucide-react'

const MIN_SCALE = 1
const MAX_SCALE = 8
const DOUBLE_CLICK_SCALE = 2.5
const BUTTON_ZOOM_FACTOR = 1.25
// Wheel sensitivity: a standard notched wheel (deltaY ≈ ±100) steps ~26% per
// tick; trackpads send many small deltas that the exponential smooths into a
// continuous zoom. Normalized below for Firefox line/page delta modes.
const WHEEL_SENSITIVITY = 0.0026
// Drag threshold (px) before a press becomes a pan. Capture is only requested
// AFTER the threshold: capturing on pointerdown would retarget the closing
// click to the overlay root, so a plain click on the zoomed image would read
// as a backdrop click and close the viewer.
const DRAG_THRESHOLD_PX = 3

// Pan clamp: keeps the scaled image covering its original fit box, so the
// image can never be dragged fully off-screen. Uses offsetWidth/Height — the
// untransformed layout size — because getBoundingClientRect would re-apply
// the current scale.
function clampOffset(img, scale, x, y) {
    const maxX = img.offsetWidth * (scale - 1) / 2
    const maxY = img.offsetHeight * (scale - 1) / 2
    return {
        x: Math.min(maxX, Math.max(-maxX, x)),
        y: Math.min(maxY, Math.max(-maxY, y)),
    }
}

function ImageLightbox({ src, alt = '', onClose }) {
    const { t } = useTranslation()
    const [scale, setScale] = useState(MIN_SCALE)
    const [offset, setOffset] = useState({ x: 0, y: 0 })
    // 'loading' | 'loaded' | 'error' — broken or hung images must leave the
    // viewer usable (close + open-original stay reachable, zoom is gated off).
    const [status, setStatus] = useState('loading')
    // Transition the transform only for button/keyboard/double-click zooms;
    // wheel and drag must track the pointer 1:1 or they feel rubber-banded.
    const [smooth, setSmooth] = useState(false)
    const [dragging, setDragging] = useState(false)

    const rootRef = useRef(null)
    const imgRef = useRef(null)
    // Ref mirrors of state read inside long-lived listeners (wheel, keyboard,
    // resize) so those effects never need to re-subscribe when zoom changes.
    const scaleRef = useRef(MIN_SCALE)
    const offsetRef = useRef({ x: 0, y: 0 })
    const statusRef = useRef('loading')
    const dragRef = useRef(null)
    const dragMovedRef = useRef(false)
    // Callers pass an inline closure; holding it in a ref keeps this
    // component render-stable (memo comparator ignores onClose) even when the
    // parent re-renders per streaming token behind the open viewer.
    const onCloseRef = useRef(onClose)
    useEffect(() => { onCloseRef.current = onClose })

    const applyZoom = useCallback((nextScale, nextOffset, smoothAnim) => {
        scaleRef.current = nextScale
        offsetRef.current = nextOffset
        setScale(nextScale)
        setOffset(nextOffset)
        setSmooth(smoothAnim)
    }, [])

    // Zoom to `targetScale` keeping the point under (clientX, clientY) fixed.
    // The anchor math reads the TRANSFORMED rect (getBoundingClientRect): the
    // translate/scale compose, so "where the image center is right now" is
    // exactly what the rect center reports.
    const zoomAt = useCallback((clientX, clientY, targetScale, smoothAnim) => {
        const img = imgRef.current
        if (!img || statusRef.current !== 'loaded') return
        const s = scaleRef.current
        const ns = Math.min(MAX_SCALE, Math.max(MIN_SCALE, targetScale))
        if (ns <= MIN_SCALE) {
            applyZoom(MIN_SCALE, { x: 0, y: 0 }, smoothAnim)
            return
        }
        let nx = offsetRef.current.x
        let ny = offsetRef.current.y
        const rect = img.getBoundingClientRect()
        if (s > 0 && rect.width > 0 && rect.height > 0) {
            const k = ns / s
            nx += (1 - k) * (clientX - (rect.left + rect.width / 2))
            ny += (1 - k) * (clientY - (rect.top + rect.height / 2))
        }
        applyZoom(ns, clampOffset(img, ns, nx, ny), smoothAnim)
    }, [applyZoom])

    const zoomAtCenter = useCallback((targetScale) => {
        zoomAt(window.innerWidth / 2, window.innerHeight / 2, targetScale, true)
    }, [zoomAt])

    // Non-passive wheel listener: preventDefault both stops page/preview
    // scroll behind the overlay and (with Ctrl held) the browser's own zoom.
    useEffect(() => {
        const el = rootRef.current
        if (!el) return
        const onWheel = (e) => {
            e.preventDefault()
            e.stopPropagation()
            const raw = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaMode === 2 ? e.deltaY * 100 : e.deltaY
            if (!Number.isFinite(raw) || raw === 0) return
            zoomAt(e.clientX, e.clientY, scaleRef.current * Math.exp(-raw * WHEEL_SENSITIVITY), false)
        }
        el.addEventListener('wheel', onWheel, { passive: false })
        return () => el.removeEventListener('wheel', onWheel)
    }, [zoomAt])

    useEffect(() => {
        const onKey = (e) => {
            if (e.key === 'Escape') {
                e.preventDefault()
                onCloseRef.current()
            } else if (e.key === '+' || e.key === '=') {
                zoomAtCenter(scaleRef.current * BUTTON_ZOOM_FACTOR)
            } else if (e.key === '-') {
                zoomAtCenter(scaleRef.current / BUTTON_ZOOM_FACTOR)
            } else if (e.key === '0') {
                zoomAtCenter(MIN_SCALE)
            }
        }
        window.addEventListener('keydown', onKey)
        return () => window.removeEventListener('keydown', onKey)
    }, [zoomAtCenter])

    // A viewport resize changes the fit box without changing scale — re-clamp
    // so the image cannot end up parked outside the new bounds.
    useEffect(() => {
        const onResize = () => {
            const img = imgRef.current
            if (!img || statusRef.current !== 'loaded') return
            const o = clampOffset(img, scaleRef.current, offsetRef.current.x, offsetRef.current.y)
            offsetRef.current = o
            setOffset(o)
        }
        window.addEventListener('resize', onResize)
        return () => window.removeEventListener('resize', onResize)
    }, [])

    // Cached images can report complete before React's onLoad listener is in
    // place; the ref callback catches that so no spinner flashes.
    const setImgRef = useCallback((node) => {
        imgRef.current = node
        if (!node) return
        if (node.complete && node.naturalWidth > 0) {
            statusRef.current = 'loaded'
            setStatus('loaded')
        } else if (node.complete && node.naturalWidth === 0) {
            statusRef.current = 'error'
            setStatus('error')
        }
    }, [])

    const onPointerDown = (e) => {
        if (e.button !== 0 || statusRef.current !== 'loaded') return
        if (scaleRef.current <= MIN_SCALE) return
        dragRef.current = {
            id: e.pointerId,
            startX: e.clientX,
            startY: e.clientY,
            baseX: offsetRef.current.x,
            baseY: offsetRef.current.y,
            captured: false,
        }
        dragMovedRef.current = false
    }

    const onPointerMove = (e) => {
        const d = dragRef.current
        if (!d || e.pointerId !== d.id) return
        const dx = e.clientX - d.startX
        const dy = e.clientY - d.startY
        if (!d.captured) {
            if (Math.abs(dx) < DRAG_THRESHOLD_PX && Math.abs(dy) < DRAG_THRESHOLD_PX) return
            d.captured = true
            dragMovedRef.current = true
            setDragging(true)
            setSmooth(false)
            try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* pointer already gone */ }
        }
        const img = imgRef.current
        if (!img) return
        const o = clampOffset(img, scaleRef.current, d.baseX + dx, d.baseY + dy)
        offsetRef.current = o
        setOffset(o)
    }

    const endDrag = (e) => {
        if (dragRef.current?.id !== e.pointerId) return
        dragRef.current = null
        setDragging(false)
        // The click following a captured drag retargets to this overlay; it
        // must already have been suppressed by dragMovedRef by the time this
        // timeout runs (click fires in the same task, before the timeout).
        setTimeout(() => { dragMovedRef.current = false }, 0)
    }

    const onBackdropClick = (e) => {
        if (e.target !== e.currentTarget) return
        if (dragMovedRef.current) {
            dragMovedRef.current = false
            return
        }
        onCloseRef.current()
    }

    const onImgDoubleClick = (e) => {
        e.preventDefault()
        if (scaleRef.current > 1.5) zoomAt(e.clientX, e.clientY, MIN_SCALE, true)
        else zoomAt(e.clientX, e.clientY, DOUBLE_CLICK_SCALE, true)
    }

    const onImgLoad = () => {
        statusRef.current = 'loaded'
        setStatus('loaded')
    }
    const onImgError = () => {
        statusRef.current = 'error'
        setStatus('error')
    }

    // Browsers block top-level navigation to data: URLs, so the anchor would
    // silently do nothing — hide it for those instead of shipping a dead
    // control. Only site-absolute and http(s) sources stay openable.
    const openableSrc = /^(?:https?:\/\/|\/)/i.test(src || '')
    const zoomDisabled = status !== 'loaded'

    return createPortal(
        <div
            ref={rootRef}
            role="dialog"
            aria-modal="true"
            aria-label={t('lightbox.title')}
            className={`fixed inset-0 z-[5000] bg-black/85 backdrop-blur-sm animate-in fade-in duration-200 select-none ${dragging ? 'cursor-grabbing' : ''}`}
            style={{ touchAction: 'none' }}
            onClick={onBackdropClick}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
        >
            <div className="absolute inset-0 flex items-center justify-center p-10 pointer-events-none">
                <img
                    ref={setImgRef}
                    src={src}
                    alt={alt}
                    draggable={false}
                    onLoad={onImgLoad}
                    onError={onImgError}
                    onDoubleClick={onImgDoubleClick}
                    className={`max-w-full max-h-full object-contain shadow-2xl pointer-events-auto ${status === 'error' ? 'hidden' : ''} ${smooth ? 'transition-transform duration-150 ease-out' : ''}`}
                    style={{
                        transform: `translate(${offset.x}px, ${offset.y}px) scale(${scale})`,
                        cursor: scale > MIN_SCALE ? (dragging ? 'grabbing' : 'grab') : 'zoom-in',
                        willChange: 'transform',
                    }}
                />
            </div>

            {status === 'loading' && (
                <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
                    <Loader2 className="w-8 h-8 text-white/60 animate-spin" />
                </div>
            )}

            {status === 'error' && (
                <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 pointer-events-none px-8 text-center">
                    <AlertTriangle className="w-10 h-10 text-amber-400/80" />
                    <p className="text-sm font-bold text-gray-200">{t('lightbox.loadFailed')}</p>
                    {alt && <p className="max-w-md truncate text-xs text-gray-400 font-mono">{alt}</p>}
                </div>
            )}

            <button
                type="button"
                onClick={(e) => { e.stopPropagation(); onCloseRef.current() }}
                title={t('lightbox.close')}
                aria-label={t('lightbox.close')}
                autoFocus
                className="absolute top-4 right-4 z-[5001] p-2.5 rounded-xl text-gray-300 hover:text-white hover:bg-white/10 transition-colors"
            >
                <X className="w-5 h-5" />
            </button>

            <div
                className="absolute bottom-6 left-1/2 -translate-x-1/2 z-[5001] flex items-center gap-0.5 bg-gray-900/85 backdrop-blur-xl border border-white/10 shadow-2xl rounded-2xl p-1.5 text-gray-300"
                onPointerDown={(e) => e.stopPropagation()}
                onClick={(e) => e.stopPropagation()}
            >
                <button
                    type="button"
                    onClick={() => zoomAtCenter(scaleRef.current / BUTTON_ZOOM_FACTOR)}
                    disabled={zoomDisabled || scale <= MIN_SCALE}
                    title={t('lightbox.zoomOut')}
                    aria-label={t('lightbox.zoomOut')}
                    className="p-2.5 rounded-xl hover:bg-white/10 hover:text-white transition-colors disabled:opacity-30 disabled:pointer-events-none"
                >
                    <ZoomOut className="w-5 h-5" />
                </button>
                <span className="w-12 text-center text-[11px] font-mono tabular-nums" aria-hidden="true">
                    {Math.round(scale * 100)}%
                </span>
                <button
                    type="button"
                    onClick={() => zoomAtCenter(scaleRef.current * BUTTON_ZOOM_FACTOR)}
                    disabled={zoomDisabled || scale >= MAX_SCALE}
                    title={t('lightbox.zoomIn')}
                    aria-label={t('lightbox.zoomIn')}
                    className="p-2.5 rounded-xl hover:bg-white/10 hover:text-white transition-colors disabled:opacity-30 disabled:pointer-events-none"
                >
                    <ZoomIn className="w-5 h-5" />
                </button>
                <div className="h-5 w-px bg-white/10 mx-1" />
                <button
                    type="button"
                    onClick={() => zoomAtCenter(MIN_SCALE)}
                    disabled={zoomDisabled}
                    title={t('lightbox.reset')}
                    aria-label={t('lightbox.reset')}
                    className="p-2.5 rounded-xl hover:bg-white/10 hover:text-white transition-colors disabled:opacity-30 disabled:pointer-events-none"
                >
                    <Maximize2 className="w-5 h-5" />
                </button>
                {openableSrc && (
                    <a
                        href={src}
                        target="_blank"
                        rel="noreferrer"
                        title={t('lightbox.openOriginal')}
                        aria-label={t('lightbox.openOriginal')}
                        className="p-2.5 rounded-xl hover:bg-white/10 hover:text-white transition-colors"
                    >
                        <ExternalLink className="w-5 h-5" />
                    </a>
                )}
            </div>
        </div>,
        document.body
    )
}

// src/alt fully determine the view; ignoring onClose identity keeps the open
// viewer from re-rendering on every parent render (chat streams re-render
// MarkdownContent per token).
export default memo(ImageLightbox, (a, b) => a.src === b.src && a.alt === b.alt)
