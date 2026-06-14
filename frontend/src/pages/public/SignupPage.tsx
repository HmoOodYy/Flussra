import { Link } from 'react-router-dom';
import styles from './SignupPage.module.css';

export function SignupPage() {
  return (
    <main className={styles.page}>
      <section className={styles.card} aria-label="Flussra beta signup placeholder">
        <Link to="/" className={styles.brandLink} aria-label="Flussra home">
          <img src="/brand/flussra-wordmark.png" alt="Flussra" className={styles.wordmark} />
        </Link>

        <div className={styles.heading}>
          <img src="/brand/flussra-mark.png" alt="" className={styles.mark} />
          <h1>Join Flussra beta</h1>
          <p>Company signup will open after beta setup.</p>
        </div>

        <form className={styles.form} aria-label="Signup placeholder form">
          <label>
            <span>Company name</span>
            <input type="text" placeholder="Your company" disabled />
          </label>
          <label>
            <span>Work email</span>
            <input type="email" placeholder="name@company.com" disabled />
          </label>
          <label>
            <span>Number of drivers</span>
            <input type="text" placeholder="Coming soon" disabled />
          </label>
          <button type="button" disabled>Request access - coming soon</button>
        </form>

        <div className={styles.links}>
          <Link to="/login">Back to login</Link>
          <Link to="/">Flussra home</Link>
        </div>
      </section>
    </main>
  );
}
