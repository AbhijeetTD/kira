/* ==========================================================
   KiraOps
   Phase 1
   script.js - Part 1
========================================================== */

document.addEventListener("DOMContentLoaded", () => {

    /* ============================================
       ON LOAD
    ============================================ */

    window.addEventListener("load", () => {

        // Recompute ScrollTrigger start/end positions once all
        // assets are loaded so reveals fire at the right spots.
        if (window.ScrollTrigger) {
            ScrollTrigger.refresh();
        }

    });

    /* ============================================
       SMOOTH SCROLL
       Native browser scrolling is GPU-accelerated and
       smooth on its own. `scroll-behavior:smooth` (in CSS)
       handles anchor links. No JS scroll library needed.
    ============================================ */

    gsap.registerPlugin(ScrollTrigger);

    /* ============================================
       CURSOR (glow + custom arrow)
    ============================================ */

    const cursor = document.querySelector(".cursor-glow");
    const arrow = document.querySelector(".cursor-arrow");
    const finePointer =
        window.matchMedia("(hover:hover) and (pointer:fine)").matches;

    if (finePointer && arrow) {

        document.documentElement.classList.add("custom-cursor");

        let mouseX = window.innerWidth / 2;
        let mouseY = window.innerHeight / 2;
        let arrowX = mouseX;
        let arrowY = mouseY;
        let seen = false;

        const interactive = (el) =>
            !!(el && el.closest && el.closest(
                'a,button,input,textarea,select,label,summary,' +
                '[role="button"],[data-demo-open],[data-action],' +
                '.nav-btn,.dashboard-sidebar li,.feature-item,.compare-card,.mini-card'
            ));

        window.addEventListener("mousemove", (e) => {

            mouseX = e.clientX;
            mouseY = e.clientY;

            if (cursor) {
                cursor.style.left = mouseX + "px";
                cursor.style.top = mouseY + "px";
            }

            if (!seen) {
                seen = true;
                arrow.classList.add("visible");
            }

            arrow.classList.toggle("hover", interactive(e.target));

        });

        window.addEventListener("mousedown", () => arrow.classList.add("down"));
        window.addEventListener("mouseup", () => arrow.classList.remove("down"));

        document.addEventListener("mouseleave", () => arrow.classList.remove("visible"));
        document.addEventListener("mouseenter", () => arrow.classList.add("visible"));

        // Smooth trailing follow (position via left/top so CSS transform
        // is free to handle the hotspot offset + hover scale).
        const followArrow = () => {
            arrowX += (mouseX - arrowX) * 0.25;
            arrowY += (mouseY - arrowY) * 0.25;
            arrow.style.left = arrowX + "px";
            arrow.style.top = arrowY + "px";
            requestAnimationFrame(followArrow);
        };
        requestAnimationFrame(followArrow);

    }

    /* ============================================
       IMMERSIVE REVEALS
       Native scrolling is kept (no scroll-jacking library)
       for a smooth, lag-free feel. Sections glide in via a
       lightweight IntersectionObserver.
    ============================================ */

    const reduceMotion =
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    if (!reduceMotion) {

        document.documentElement.classList.add("smooth-reveal");

        const revealEls = document.querySelectorAll(
            ".section-heading,.section-title,.feature-item,.compare-card," +
            ".mini-card,.dashboard-grid,.incident-flow,.architecture-wrapper," +
            ".console-window,.cta-content,.footer-brand,.footer-col"
        );

        const io = new IntersectionObserver((entries) => {
            entries.forEach((entry) => {
                if (entry.isIntersecting) {
                    entry.target.classList.add("in-view");
                    io.unobserve(entry.target);
                }
            });
        }, { threshold: 0.12, rootMargin: "0px 0px -6% 0px" });

        revealEls.forEach((el, i) => {
            el.classList.add("reveal-up");
            el.style.transitionDelay = Math.min((i % 4) * 0.06, 0.18) + "s";
            io.observe(el);
        });

        // Safety net: never leave visible content hidden if the observer
        // fails to fire for any reason.
        setTimeout(() => {
            document.querySelectorAll(".reveal-up:not(.in-view)").forEach((el) => {
                if (el.getBoundingClientRect().top < window.innerHeight) {
                    el.classList.add("in-view");
                }
            });
        }, 4000);
    }

    /* ============================================
       NAVBAR
    ============================================ */

    const navbar = document.querySelector(".navbar");

    window.addEventListener("scroll", () => {

        if (window.scrollY > 40) {

            navbar.classList.add("scrolled");

        } else {

            navbar.classList.remove("scrolled");

        }

    });

    /* ============================================
       SCROLL REVEAL
    ============================================ */

    const revealElements = document.querySelectorAll(".placeholder");

    const observer = new IntersectionObserver((entries) => {

        entries.forEach(entry => {

            if (entry.isIntersecting) {

                entry.target.classList.add("show");

            }

        });

    }, {

        threshold: .15

    });

    revealElements.forEach(el => {

        el.classList.add("fade-up");

        observer.observe(el);

    });

});
/* ==========================================================
   AI CORE INTERACTIONS
========================================================== */

const hero = document.querySelector(".hero");
const core = document.querySelector(".core");

if (hero && core) {

    hero.addEventListener("mousemove", (e) => {

        const rect = hero.getBoundingClientRect();

        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;

        const rotateY = ((x / rect.width) - 0.5) * 18;
        const rotateX = ((y / rect.height) - 0.5) * -18;

        gsap.to(core, {
            rotationY: rotateY,
            rotationX: rotateX,
            x: (rotateY * 1.5),
            y: (rotateX * -1.5),
            duration: .8,
            ease: "power3.out"
        });

    });

    hero.addEventListener("mouseleave", () => {

        gsap.to(core, {
            rotationX: 0,
            rotationY: 0,
            x: 0,
            y: 0,
            duration: 1,
            ease: "power3.out"
        });

    });

}

/* ==========================================================
   FLOATING AI NODES
========================================================== */

gsap.utils.toArray(".node").forEach((node, index) => {

    gsap.to(node, {

        y: "+=12",

        duration: 2 + (index * .4),

        repeat: -1,

        yoyo: true,

        ease: "sine.inOut"

    });

});

/* ==========================================================
   MAGNETIC BUTTON EFFECT
========================================================== */

document.querySelectorAll(".btn-primary,.btn-secondary,.nav-btn")
.forEach(button => {

    button.addEventListener("mousemove", e => {

        const rect = button.getBoundingClientRect();

        const x = e.clientX - rect.left - rect.width / 2;

        const y = e.clientY - rect.top - rect.height / 2;

        gsap.to(button, {

            x: x * .18,

            y: y * .18,

            duration: .3,

            ease: "power2.out"

        });

    });

    button.addEventListener("mouseleave", () => {

        gsap.to(button, {

            x: 0,

            y: 0,

            duration: .5,

            ease: "elastic.out(1,0.4)"

        });

    });

});

/* ==========================================================
   GLOWING CORE
========================================================== */

setInterval(() => {

    gsap.to(".core-center", {

        scale: 1.08,

        boxShadow:

            "0 0 45px rgba(79,140,255,.95),0 0 140px rgba(34,211,238,.45)",

        duration: .8,

        yoyo: true,

        repeat: 1,

        ease: "power1.inOut"

    });

}, 3500);

/* ==========================================================
   RANDOM NODE GLOW
========================================================== */

const nodes = document.querySelectorAll(".node");

setInterval(() => {

    const node = nodes[Math.floor(Math.random() * nodes.length)];

    gsap.fromTo(node,

        {

            boxShadow: "0 0 0 rgba(79,140,255,0)"

        },

        {

            boxShadow: "0 0 30px rgba(79,140,255,.9)",

            duration: .5,

            yoyo: true,

            repeat: 1

        }

    );

}, 1800);

/* ==========================================================
   HERO TITLE STAGGER LOOP
========================================================== */

const heroLines = document.querySelectorAll(".hero h1");

gsap.to(heroLines, {

    opacity: .95,

    duration: 2,

    repeat: -1,

    yoyo: true,

    ease: "sine.inOut"

});

/* ==========================================================
   PARTICLE PARALLAX
========================================================== */

const particles = document.querySelector(".particles");

window.addEventListener("mousemove", e => {

    const x = (e.clientX / window.innerWidth - .5) * 20;

    const y = (e.clientY / window.innerHeight - .5) * 20;

    gsap.to(particles, {

        x,

        y,

        duration: 1.5,

        ease: "power3.out"

    });

});

/* ==========================================================
   PERFORMANCE
========================================================== */

gsap.ticker.lagSmoothing(1000, 16);

/* ==========================================================
   END OF PHASE 1
========================================================== */
/* ==========================================================
   PRODUCT EXPERIENCE ANIMATIONS
========================================================== */

document.addEventListener("DOMContentLoaded", () => {

    gsap.registerPlugin(ScrollTrigger);

    /* ============================================
       Floating AI Engine
    ============================================ */

    gsap.to(".brain-core", {
        y: -12,
        duration: 2,
        repeat: -1,
        yoyo: true,
        ease: "sine.inOut"
    });

    /* ============================================
       Animated Metric Bars
    ============================================ */

    gsap.fromTo(".cpu-fill",
        { width: "0%" },
        {
            width: "94%",
            duration: 2,
            ease: "power2.out",
            scrollTrigger: {
                trigger: ".metrics-card",
                start: "top 80%"
            }
        });

    gsap.fromTo(".mem-fill",
        { width: "0%" },
        {
            width: "91%",
            duration: 2,
            delay: 0.2,
            ease: "power2.out",
            scrollTrigger: {
                trigger: ".metrics-card",
                start: "top 80%"
            }
        });

    gsap.fromTo(".latency-fill",
        { width: "0%" },
        {
            width: "86%",
            duration: 2,
            delay: 0.4,
            ease: "power2.out",
            scrollTrigger: {
                trigger: ".metrics-card",
                start: "top 80%"
            }
        });

    /* ============================================
       Confidence Counter
    ============================================ */

    const confidence = document.querySelector(".confidence strong");

    if (confidence) {

        let obj = { value: 0 };

        ScrollTrigger.create({

            trigger: ".analysis-card",

            start: "top 75%",

            once: true,

            onEnter: () => {

                gsap.to(obj, {

                    value: 98.7,

                    duration: 2,

                    ease: "power2.out",

                    onUpdate: () => {

                        confidence.textContent =
                            obj.value.toFixed(1) + "%";

                    }

                });

            }

        });

    }

    /* ============================================
       Live Log Streaming
    ============================================ */

    const terminal = document.querySelector(".logs-card pre");

    if (terminal) {

        const lines = [
            "ERROR Payment API timeout",
            "ERROR Redis connection lost",
            "WARN Retry attempt #5",
            "ERROR OOMKilled pod/api-4",
            "INFO Scaling deployment...",
            "INFO New replica created",
            "INFO Health probe passed",
            "SUCCESS Traffic stabilized"
        ];

        let index = 0;

        setInterval(() => {

            terminal.textContent += "\n" + lines[index];

            terminal.scrollTop = terminal.scrollHeight;

            index = (index + 1) % lines.length;

            if (terminal.textContent.length > 1200) {

                terminal.textContent = lines.slice(-5).join("\n");

            }

        }, 2500);

    }

    /* ============================================
       Card Glow on Hover
    ============================================ */

    document.querySelectorAll(
        ".telemetry-card,.analysis-card,.recommendation,.automation-terminal"
    ).forEach(card => {

        card.addEventListener("mouseenter", () => {

            gsap.to(card, {

                boxShadow:
                    "0 0 60px rgba(79,140,255,.25)",

                duration: 0.3

            });

        });

        card.addEventListener("mouseleave", () => {

            gsap.to(card, {

                boxShadow:
                    "0 20px 60px rgba(79,140,255,.10)",

                duration: 0.3

            });

        });

    });

});
/* ==========================================================
   DASHBOARD ANIMATIONS
========================================================== */

document.addEventListener("DOMContentLoaded", () => {

    /* ============================================
       Floating Dashboard
    ============================================ */

    gsap.to(".dashboard-main", {
        y: -8,
        repeat: -1,
        yoyo: true,
        duration: 4,
        ease: "sine.inOut"
    });

    /* ============================================
       Animated Health Chart
    ============================================ */

    gsap.from(".chart-line", {
        scaleX: 0,
        transformOrigin: "left center",
        duration: 2,
        ease: "power2.out",
        scrollTrigger: {
            trigger: ".health-chart",
            start: "top 80%"
        }
    });

    /* ============================================
       Dashboard Counter Animation
    ============================================ */

    document.querySelectorAll(".mini-card strong").forEach(counter => {

        if (counter.classList.contains("danger-count")) return;

        const target = parseInt(counter.textContent);

        if (isNaN(target)) return;

        ScrollTrigger.create({

            trigger: ".dashboard-section",

            start: "top 70%",

            once: true,

            onEnter: () => {

                let obj = { value: 0 };

                gsap.to(obj, {

                    value: target,

                    duration: 2,

                    ease: "power2.out",

                    onUpdate: () => {

                        counter.textContent =
                            Math.floor(obj.value);

                    }

                });

            }

        });

    });

    /* ============================================
       Live Incident Rotation
    ============================================ */

    const incidents = [
        ["payment-prod", "Critical"],
        ["inventory-api", "Warning"],
        ["redis-cache", "Recovered"],
        ["auth-service", "Resolved"],
        ["orders-db", "Investigating"],
        ["gateway-prod", "Healthy"]
    ];

    const list = document.querySelector(".incident-list");

    if (list) {

        const maxItems = list.querySelectorAll("li").length || 3;

        let index = 0;

        let rotating = false;

        setInterval(() => {

            const items = list.querySelectorAll("li");

            if (!items.length || rotating) return;

            rotating = true;

            gsap.to(items[0], {

                opacity: 0,

                y: -20,

                duration: .3,

                onComplete: () => {

                    items[0].remove();

                    const li = document.createElement("li");

                    li.innerHTML =
                        `${incidents[index][0]} <span>${incidents[index][1]}</span>`;

                    li.style.opacity = 0;

                    list.appendChild(li);

                    // Safety cap: never let the list grow beyond its original size.
                    while (list.children.length > maxItems) {
                        list.firstElementChild.remove();
                    }

                    gsap.to(li, {

                        opacity: 1,

                        y: 0,

                        duration: .5

                    });

                    index = (index + 1) % incidents.length;

                    rotating = false;

                }

            });

        }, 4000);

    }

    /* ============================================
       Sidebar Active Effect
    ============================================ */

    document.querySelectorAll(".dashboard-sidebar li").forEach(item => {

        item.addEventListener("click", () => {

            document.querySelectorAll(".dashboard-sidebar li")
                .forEach(i => i.classList.remove("active"));

            item.classList.add("active");

        });

    });

});
/* ==========================================================
   ARCHITECTURE ANIMATIONS
========================================================== */

document.addEventListener("DOMContentLoaded", () => {

    /* --------------------------------------------
       Floating Center
    -------------------------------------------- */

    gsap.to(".arch-center", {

        y: -10,

        duration: 3,

        repeat: -1,

        yoyo: true,

        ease: "sine.inOut"

    });

    /* --------------------------------------------
       Pulse Glow
    -------------------------------------------- */

    gsap.to(".core-glow", {

        scale: 1.15,

        opacity: .45,

        duration: 2,

        repeat: -1,

        yoyo: true,

        ease: "sine.inOut"

    });

    /* --------------------------------------------
       Floating Cards
    -------------------------------------------- */

    document.querySelectorAll(".arch-node").forEach((node, index) => {

        gsap.to(node, {

            y: -10,

            duration: 2 + (index * .15),

            repeat: -1,

            yoyo: true,

            ease: "sine.inOut"

        });

    });

    /* --------------------------------------------
       Orbit Rotation (handled via CSS animation)
    -------------------------------------------- */

});
/* ==========================================================
   FEATURES TIMELINE ANIMATIONS
========================================================== */

document.addEventListener("DOMContentLoaded", () => {

    /* --------------------------------------------
       Floating Number Badges
    -------------------------------------------- */

    document.querySelectorAll(".feature-number").forEach((badge, index) => {

        gsap.to(badge, {

            y: -5,

            duration: 1.8 + index * 0.15,

            repeat: -1,

            yoyo: true,

            ease: "sine.inOut"

        });

    });

    /* --------------------------------------------
       Hover Glow
    -------------------------------------------- */

    document.querySelectorAll(".feature-item").forEach(card => {

        card.addEventListener("mouseenter", () => {

            gsap.to(card, {

                boxShadow: "0 20px 60px rgba(90,140,255,.25)",

                duration: .3

            });

        });

        card.addEventListener("mouseleave", () => {

            gsap.to(card, {

                boxShadow: "0 0 0 rgba(0,0,0,0)",

                duration: .3

            });

        });

    });

});
/* ==========================================================
   COMPARISON ANIMATIONS
========================================================== */

document.addEventListener("DOMContentLoaded", () => {

    /* --------------------------------------------
       Pulse MTTR Badge
    -------------------------------------------- */

    gsap.to(".good strong", {

        scale: 1.08,

        duration: 1.2,

        repeat: -1,

        yoyo: true,

        ease: "sine.inOut"

    });

    /* --------------------------------------------
       Highlight KIRA Card
    -------------------------------------------- */

    gsap.to(".kira", {

        boxShadow: "0 0 45px rgba(90,150,255,.22)",

        duration: 2,

        repeat: -1,

        yoyo: true,

        ease: "sine.inOut"

    });

});
/* ==========================================================
   KIRA TERMINAL
========================================================== */

document.addEventListener("DOMContentLoaded", () => {

    const output = document.getElementById("terminal-output");

    if (!output) return;

    const steps = [
        "$ kira analyze payment-service",
        "",
        "Connecting to Kubernetes cluster...",
        "✓ Connected",
        "",
        "Collecting Prometheus metrics...",
        "✓ Metrics collected",
        "",
        "Reading Loki logs...",
        "✓ Logs correlated",
        "",
        "Inspecting Kubernetes events...",
        "✓ Events analyzed",
        "",
        "Reading OpenTelemetry traces...",
        "✓ Traces correlated",
        "",
        "──────────────────────────────",
        "",
        "ROOT CAUSE",
        "OOMKilled",
        "",
        "CONFIDENCE",
        "98.7%",
        "",
        "RECOMMENDATION",
        "Increase memory limit to 768Mi",
        "",
        "ESTIMATED RECOVERY",
        "42 seconds",
        "",
        "STATUS",
        "Ready for automation ✓"
    ];

    output.textContent = "";

    let line = 0;

    function typeLine() {

        if (line >= steps.length) return;

        output.textContent += steps[line] + "\n";

        output.scrollTop = output.scrollHeight;

        line++;

        setTimeout(typeLine, 220);

    }

    ScrollTrigger.create({

        trigger: ".console-section",

        start: "top 70%",

        once: true,

        onEnter: typeLine

    });

    /* Button Pulse */

    gsap.to(".terminal-btn", {

        scale: 1.05,

        duration: 1.3,

        repeat: -1,

        yoyo: true,

        ease: "sine.inOut"

    });

});
/* ==========================================================
   LIVE DEMO MODAL
========================================================== */

document.addEventListener("DOMContentLoaded", () => {

    const modal = document.getElementById("demoModal");

    if (!modal) return;

    const screens = modal.querySelectorAll(".demo-screen");
    const trackItems = modal.querySelectorAll(".demo-track-item");
    const navItems = modal.querySelectorAll(".demo-nav a");
    const progressBar = modal.querySelector(".demo-progress-bar");
    const confValue = modal.querySelector(".demo-conf-value");
    const checks = modal.querySelectorAll(".demo-check");
    const actionNote = modal.querySelector("[data-note]");

    let step = -1;
    let timers = [];

    const clearTimers = () => {
        timers.forEach(clearTimeout);
        timers = [];
    };

    const later = (fn, ms) => {
        const t = setTimeout(fn, ms);
        timers.push(t);
        return t;
    };

    function setStep(n) {

        step = n;

        screens.forEach((s, i) => s.classList.toggle("active", i === n));

        trackItems.forEach((t, i) => {
            t.classList.toggle("active", i === n);
            t.classList.toggle("done", i < n);
        });

        navItems.forEach((a, i) => a.classList.toggle("active", i === n));

        progressBar.style.width = (n / (screens.length - 1)) * 100 + "%";
    }

    function runRCA() {

        checks.forEach(c => c.classList.remove("done"));

        checks.forEach((c, i) => later(() => c.classList.add("done"), 350 + i * 450));

        confValue.textContent = "0%";

        later(() => {
            const target = 98.7;
            let v = 0;
            const iv = setInterval(() => {
                v += target / 24;
                if (v >= target) { v = target; clearInterval(iv); }
                confValue.textContent = v.toFixed(1) + "%";
            }, 55);
            timers.push(iv);
        }, 350 + checks.length * 450);
    }

    function resolve(kind) {

        clearTimers();

        if (actionNote) {
            actionNote.textContent = {
                apply:   "⚡ Applying fix automatically…",
                auto:    "⚡ No response within SLA — KIRA auto-applying the fix…",
                approve: "✅ Approval requested from the on-call engineer…",
                manual:  "🔧 Opening manual runbook for production review…"
            }[kind] || "";
        }

        later(() => setStep(4), (kind === "approve" || kind === "manual") ? 1500 : 950);
    }

    function play() {

        clearTimers();

        if (actionNote) actionNote.textContent = "";

        setStep(0);

        later(() => setStep(1), 3200);

        later(() => { setStep(2); runRCA(); }, 6400);

        later(() => setStep(3), 11400);

        // Auto-remediate if the viewer doesn't pick an option
        later(() => { if (step === 3) resolve("auto"); }, 18000);
    }

    function openDemo() {

        modal.classList.add("open");
        modal.setAttribute("aria-hidden", "false");
        document.body.classList.add("demo-lock");
        play();
    }

    function closeDemo() {

        modal.classList.remove("open");
        modal.setAttribute("aria-hidden", "true");
        document.body.classList.remove("demo-lock");
        clearTimers();
    }

    document.querySelectorAll("[data-demo-open]").forEach(btn =>
        btn.addEventListener("click", e => { e.preventDefault(); openDemo(); })
    );

    modal.querySelectorAll("[data-demo-close]").forEach(btn =>
        btn.addEventListener("click", closeDemo)
    );

    const replayBtn = modal.querySelector("[data-demo-replay]");
    if (replayBtn) replayBtn.addEventListener("click", play);

    modal.querySelectorAll("[data-action]").forEach(btn =>
        btn.addEventListener("click", () => resolve(btn.dataset.action))
    );

    document.addEventListener("keydown", e => {
        if (e.key === "Escape" && modal.classList.contains("open")) closeDemo();
    });
});