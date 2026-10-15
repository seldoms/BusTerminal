import './LayoutPrimitives.css';

function ContentContainer({ children, className = '', ...props }) {
    const classes = ['content-container', className].filter(Boolean).join(' ');
    return (
        <div className={classes} {...props}>
            {children}
        </div>
    );
}

export default ContentContainer;
