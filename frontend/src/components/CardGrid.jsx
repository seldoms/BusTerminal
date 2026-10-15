import './LayoutPrimitives.css';

function CardGrid({ children, className = '', variant = 'default', ...props }) {
    const variantClass = variant === 'default' ? '' : `card-grid-${variant}`;
    const classes = ['card-grid', variantClass, className].filter(Boolean).join(' ');
    return (
        <div className={classes} {...props}>
            {children}
        </div>
    );
}

export default CardGrid;
