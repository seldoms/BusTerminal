import React, { useState } from 'react';
import { Monitor, Grid, LayoutGrid } from 'lucide-react';
import { CARD_WIDTH_MIN, CARD_WIDTH_MAX, getGridGap, getContentMaxWidth } from '../../layoutSettings';

export default function SettingsLayoutTab({ cardWidth, onChangeLayout }) {
    // 拖动中只更新卡片宽度相关 CSS 变量（preview 模式），
    // 避免页面容器宽度跟着跳导致滑块在鼠标下"跑动"；松手时才完整应用布局
    const [dragWidth, setDragWidth] = useState(null);
    const displayWidth = dragWidth ?? cardWidth;
    const currentContainerWidth = getContentMaxWidth(displayWidth);
    const gridGap = getGridGap(displayWidth);

    const handleSliderChange = (e) => {
        const nextWidth = Number(e.target.value);
        setDragWidth(nextWidth);
        onChangeLayout({ cardWidth: nextWidth }, { preview: true, persist: false, notify: false });
    };

    const commitSlider = () => {
        if (dragWidth === null) return;
        onChangeLayout({ cardWidth: dragWidth });
        setDragWidth(null);
    };

    return (
        <div className="settings-tab-pane">
            <div className="settings-card">
                <div className="card-body">
                    {/* Slider Control */}
                    <div className="setting-control-group">
                        <div className="control-label-row">
                            <label className="control-label">
                                <Grid size={16} style={{ marginRight: 6 }} />
                                卡片尺寸与屏宽联动
                            </label>
                            <span className="control-value-badge">
                                卡片 {displayWidth}px | 页面最大宽 {currentContainerWidth}
                            </span>
                        </div>

                        <div className="slider-wrapper">
                            <span className="slider-hint">
                                <Monitor size={14} /> 小封面 · 宽内容区 ({CARD_WIDTH_MIN}px)
                            </span>
                            <input
                                type="range"
                                min={CARD_WIDTH_MIN}
                                max={CARD_WIDTH_MAX}
                                step={5}
                                value={displayWidth}
                                onChange={handleSliderChange}
                                onPointerUp={commitSlider}
                                onKeyUp={commitSlider}
                                onBlur={commitSlider}
                                className="settings-slider"
                            />
                            <span className="slider-hint">
                                宽屏 ({CARD_WIDTH_MAX}px) <LayoutGrid size={14} />
                            </span>
                        </div>
                    </div>

                    {/* Preview Box */}
                    <div className="layout-preview-box">
                        <div className="preview-header">
                            <span>布局实时预览演示</span>
                            <span className="preview-info">当前间距: {gridGap}px</span>
                        </div>
                        <div
                            className="preview-grid"
                            style={{
                                display: 'grid',
                                gridTemplateColumns: `repeat(auto-fill, minmax(${displayWidth}px, 1fr))`,
                                gap: `${gridGap}px`
                            }}
                        >
                            {[1, 2, 3, 4].map((item) => (
                                <div key={item} className="preview-card-item">
                                    <div className="preview-poster-dummy">
                                        <span>Poster #{item}</span>
                                    </div>
                                    <div className="preview-card-text">
                                        <div className="dummy-line long"></div>
                                        <div className="dummy-line short"></div>
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}
