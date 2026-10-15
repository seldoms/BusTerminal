import React, { useEffect, useState } from 'react';
import './HeroParallax.css';

function HeroParallax({ title, subtitle, children }) {
    const [offset, setOffset] = useState(0);

    useEffect(() => {
        // Disable parallax on mobile or if reduced motion is preferred
        const isMobile = window.innerWidth < 768;
        const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

        if (isMobile || prefersReducedMotion) return;

        const handleScroll = () => {
            // Clamping or simple setting
            // Use requestAnimationFrame for smoother performance? 
            // React state updates might be slightly laggy for scroll, but usually okay for simple simple layout.
            // For strict performance, using useRef + direct DOM manipulation is better, but K.I.S.S principle first.
            // However, for 60fps, we should throttle or use raf.
            requestAnimationFrame(() => {
                setOffset(window.scrollY);
            });
        };

        window.addEventListener('scroll', handleScroll);
        return () => window.removeEventListener('scroll', handleScroll);
    }, []);

    return (
        <div className="hero-parallax">
            {/* Layer 1: Background - Moves slower (Speed 0.5) */}
            <div
                className="parallax-layer layer-bg"
                style={{ transform: `translate3d(0, ${offset * 0.5}px, 0)` }}
            ></div>

            {/* Layer 2: Midground Shapes - Moves slower but different (Speed 0.2) */}
            <div
                className="parallax-layer layer-mid"
                style={{ transform: `translate3d(0, ${offset * 0.2}px, 0)` }}
            >
                <div className="shape shape-1"></div>
                <div className="shape shape-2"></div>
            </div>

            {/* Layer 3: Content - Normal Flow (or slightly offset if we want reverse parallax, but usually static relative to container is easier, sticky container?) 
                Actually, standard parallax hero usually has fixed bg. 
                Here we translate layers down as we scroll down to simulate depth. 
                Background moving slower than scroll = depth.
                Content moving normal = foreground.
            */}
            <div className="layer-content">
                <h1 className="hero-title">{title}</h1>
                <p className="hero-subtitle">{subtitle}</p>
                {children}
            </div>
        </div>
    );
}

export default HeroParallax;
