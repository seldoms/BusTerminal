import React, { useEffect, useState } from 'react';
import { Heart } from 'lucide-react';
import { Link } from 'react-router-dom';
import {
    fallbackAvatar,
    formatPlainValue,
    getActressAvatarSrc,
    getActressDetailPath,
    getActressDisplayName,
    getCompactActressName,
    getAgeBadge,
    getCupBadge,
    getHeightLabel,
} from '../utils/actressDisplay';

const CupBadge = ({ actress }) => {
    const badge = getCupBadge(actress);
    if (!badge) return null;

    return (
        <span
            className={`cup-badge${badge.estimated ? ' estimated' : ''}`}
            title={badge.title}
        >
            {badge.label}
        </span>
    );
};

const AgeBadge = ({ actress }) => {
    const badge = getAgeBadge(actress);
    if (!badge) return null;

    return (
        <span className="age-badge" title={badge.title}>
            {badge.label}
        </span>
    );
};

function ActressCard({ actress, isLast = false, lastElementRef, onToggleSubscription }) {
    const [imageFailed, setImageFailed] = useState(false);
    const avatar = getActressAvatarSrc(actress?.avatar);
    const imageSrc = imageFailed ? fallbackAvatar : avatar;
    const displayName = getActressDisplayName(actress);
    const compactName = getCompactActressName(actress);
    const heightLabel = getHeightLabel(actress?.height);
    const birthDate = formatPlainValue(actress?.birth_date);
    const isSubscribed = actress?.is_followed === true || actress?.is_followed === 1 || actress?.is_followed === '1';
    const duplicateCount = Number(actress?.dup_count) || 0;

    useEffect(() => {
        setImageFailed(false);
    }, [actress?.avatar]);

    const handleToggleSubscription = (event) => {
        event.preventDefault();
        event.stopPropagation();
        onToggleSubscription(actress.id);
    };

    return (
        <div
            className={`actress-card-modern ${actress.is_new ? 'new-tea-card' : ''}`}
            ref={isLast ? lastElementRef : null}
        >
            <div className="avatar-container">
                {actress.is_new && <div className="tea-badge">新茶</div>}
                <Link to={getActressDetailPath(actress.id)} aria-label={`查看 ${displayName}`}>
                    <img
                        src={imageSrc}
                        alt={displayName}
                        loading="lazy"
                        decoding="async"
                        onError={() => setImageFailed(true)}
                    />
                </Link>
                <AgeBadge actress={actress} />
                <CupBadge actress={actress} />
                <button
                    type="button"
                    className={`subscription-overlay ${isSubscribed ? 'active' : ''}`}
                    onClick={handleToggleSubscription}
                    title={isSubscribed ? '取消订阅' : '订阅'}
                    aria-label={isSubscribed ? `取消订阅 ${displayName}` : `订阅 ${displayName}`}
                >
                    {isSubscribed ? <Heart size={16} /> : <Heart size={16} fill="none" />}
                </button>
            </div>

            <div className="actress-details-modern">
                <div className="actress-summary-row">
                    <Link
                        to={getActressDetailPath(actress.id)}
                        className="actress-name-link"
                        title={displayName}
                    >
                        {compactName}
                    </Link>
                    {heightLabel && <span className="actress-height">{heightLabel}</span>}
                    {duplicateCount > 1 && (
                        <span
                            className="actress-dup-badge"
                            title={`库内有 ${duplicateCount} 条同名记录（多来自不同来源的同一人），点开可看到全部影片`}
                        >
                            同名 ×{duplicateCount}
                        </span>
                    )}
                </div>
                {birthDate && <div className="actress-birth-date">{birthDate}</div>}
            </div>
        </div>
    );
}

export default ActressCard;
