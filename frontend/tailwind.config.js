/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{vue,js}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        clinical: {
          50: "#eef7f3",
          100: "#d8ede3",
          500: "#2f9e73",
          600: "#1f7a58",
          700: "#176045",
        },
      },
    },
  },
  plugins: [],
};
